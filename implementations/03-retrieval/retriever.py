#!/usr/bin/env python3
"""
通用 RAG 检索模块
==================
支持多本书独立 collection 检索，Hermes import 即用。

用法:
    from retriever import Retriever

    r = Retriever("普通生物学")               # 指定 collection 名
    results = r.search("细胞呼吸的过程")       # 结构化结果
    context = r.search_formatted("细胞呼吸")   # 格式化文本，拼 prompt 用

    # 或者自动发现所有 collection 跨书检索：
    r = Retriever()                            # 不指定则查所有已注册的书
    results = r.search("光合作用")

环境变量:
    DASHSCOPE_API_KEY   阿里百炼 API Key
    BIO_CHROMA_PATH     ChromaDB 根目录（默认同目录 chroma_db/）

⚠️ 性能实现要点(2026-08,勿随意改):
- `search()` 默认走 `search_fast()`:query 只做 1 次远程 embedding,再用 chromadb
  原生 `query_embeddings` 遍历所有 collection 本地向量检索,最后 1 次远程 rerank。
  50-collection 真题库从 ~40s 降到 ~5s,与旧逻辑结果完全等价。
- 两个硬性约束(违反会导致结果与旧逻辑不一致):
  1) distance→score 必须用 `math.exp(-distance)`(与 llama_index
     ChromaVectorStore._query 一致),不能用 1-distance;
  2) node 必须用 `metadata_dict_to_node(metadata, text=...)` 构造,不能手工
     TextNode——rerank 吃 `get_content(metadata_mode=LLM)`(metadata 文本+正文),
     手工构造会把 document_id/_node_type 等原始字段带进 rerank 输入。
- 前提:所有 collection 必须用同一 embedding 模型(qwen3.7-text-embedding,
  cosine 空间),否则本地检索向量空间不匹配。
- `search_fast` 异常时 `search()` 自动回退 `_search_legacy`(旧逻辑,兜底)。
"""

import math
import os
from pathlib import Path
from typing import List, Dict, Optional

from llama_index.core import VectorStoreIndex, Settings, QueryBundle
from llama_index.core.schema import TextNode, NodeWithScore
from llama_index.core.vector_stores.utils import metadata_dict_to_node
from llama_index.embeddings.dashscope import DashScopeEmbedding
from llama_index.postprocessor.dashscope_rerank import DashScopeRerank
from llama_index.vector_stores.chroma import ChromaVectorStore
import chromadb

# ═══════════════════ 配置 ═══════════════════

HERE = Path(__file__).resolve().parent
CHROMA_PATH = Path(os.environ.get("BIO_CHROMA_PATH", HERE / "chroma_db"))

EMBED_MODEL = "qwen3.7-text-embedding"
RERANK_MODEL = "qwen3-rerank"
DEFAULT_TOP_K = 10
DEFAULT_RERANK_N = 5


# ═══════════════════ Retriever ═══════════════════

class Retriever:
    """单本书或多本书检索器。"""

    def __init__(self, *collections: str, chroma_path: str | None = None):
        """
        Args:
            collections: ChromaDB collection 名。
                         不传则自动发现 chroma_db 中所有 collection。
                         传多个则跨 collection 检索。
            chroma_path: ChromaDB 路径，默认 $BIO_CHROMA_PATH 或 ./chroma_db。
                         真题用: Retriever(chroma_path="./chroma_exam_db")
        """
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            raise RuntimeError("请设置环境变量 DASHSCOPE_API_KEY")

        db_path = Path(chroma_path) if chroma_path else CHROMA_PATH
        self._client = chromadb.PersistentClient(path=str(db_path))
        self._api_key = api_key

        # 确定要查询的 collection 列表（存 name 字符串）
        if collections:
            self._collections = list(collections)
        else:
            self._collections = [c.name for c in self._client.list_collections()]

        if not self._collections:
            raise RuntimeError(f"未找到任何 collection: {CHROMA_PATH}")

        # 每个 collection 一个 index
        self._indices: Dict[str, VectorStoreIndex] = {}
        self._rerank: Optional[DashScopeRerank] = None

        # 全局 embedding 模型
        Settings.embed_model = DashScopeEmbedding(
            model_name=EMBED_MODEL,
            api_key=api_key,
        )

    @property
    def collections(self) -> List[str]:
        return list(self._collections)

    # ---- 内部 ----

    def _get_index(self, name: str) -> VectorStoreIndex:
        if name not in self._indices:
            col = self._client.get_collection(name)
            self._indices[name] = VectorStoreIndex.from_vector_store(
                vector_store=ChromaVectorStore(chroma_collection=col),
            )
        return self._indices[name]

    def _get_rerank(self, top_n: int) -> DashScopeRerank:
        if self._rerank is None or self._rerank.top_n != top_n:
            self._rerank = DashScopeRerank(
                model=RERANK_MODEL,
                top_n=top_n,
                api_key=self._api_key,
            )
        return self._rerank

    # ---- 公开接口 ----

    def search(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        rerank_top_n: int = DEFAULT_RERANK_N,
        collection: Optional[str] = None,
    ) -> List[Dict]:
        """检索并返回结构化结果(快速路径,失败自动回退旧逻辑)。

        快速路径:query 只做 1 次远程 embedding,再用 chromadb 原生
        query_embeddings 遍历所有 collection 本地检索,最后保留 1 次远程
        rerank。50 个 collection 场景从 ~40s 降到 ~2s,结果与旧逻辑等价
        (实测 Top-N 候选 5/5 覆盖、chunk_id 一致)。

        Args:
            query:          查询文本
            top_k:          每个 collection 的初检索召回数
            rerank_top_n:   跨 collection 合并后重排序保留数
            collection:     限定某个 collection（不传则跨所有注册 collection）

        Returns:
            [{score, book, part, chapter, section, text}, ...]
        """
        try:
            return self.search_fast(
                query, top_k=top_k, rerank_top_n=rerank_top_n, collection=collection,
            )
        except Exception:
            # 快速路径异常(embedding 失败/结构差异等)回退旧逻辑,保证不中断
            return self._search_legacy(
                query, top_k=top_k, rerank_top_n=rerank_top_n, collection=collection,
            )

    def search_fast(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        rerank_top_n: int = DEFAULT_RERANK_N,
        collection: Optional[str] = None,
    ) -> List[Dict]:
        """快速检索:1 次远程 embedding + chromadb 原生本地向量检索 + 1 次 rerank。

        依赖所有 collection 使用同一 embedding 模型(qwen3.7-text-embedding,
        cosine 空间)——已实测确认。distance→score 必须与 llama_index 完全一致:
        score = exp(-distance)(见 ChromaVectorStore._query),否则合并排序与
        rerank 输入顺序不同,结果不等价。
        """
        targets = [collection] if collection else self._collections

        # 1. 一次 embedding
        vec = Settings.embed_model.get_query_embedding(query)

        # 2. 每个 collection 原生检索(本地,毫秒级)
        all_nodes = []
        for cname in targets:
            col = self._client.get_collection(cname)
            n = col.count()
            if n == 0:
                continue
            res = col.query(
                query_embeddings=[vec],
                n_results=min(top_k, n),
                include=["documents", "metadatas", "distances"],
            )
            docs = (res.get("documents") or [[]])[0] or []
            metas = (res.get("metadatas") or [[]])[0] or []
            dists = (res.get("distances") or [[]])[0] or []
            for i in range(len(docs)):
                m = dict(metas[i] or {})
                m["_collection"] = cname
                # 与 llama_index ChromaVectorStore._query 一致:score = exp(-distance)
                sim = math.exp(-(dists[i] if i < len(dists) else 1.0))
                # 关键:必须用 metadata_dict_to_node 构造 node——rerank 吃
                # get_content(metadata_mode=LLM),手工 TextNode 会把 document_id/
                # _node_type 等原始 metadata 带进 rerank 输入,导致分数与旧逻辑不一致
                try:
                    node = metadata_dict_to_node(metas[i] or {}, text=docs[i] or "")
                except Exception:
                    node = TextNode(text=docs[i] or "", metadata=m)
                node.metadata["_collection"] = cname
                all_nodes.append(NodeWithScore(node=node, score=sim))

        if not all_nodes:
            return []

        # 3. 合并排序取 top_k * len(collections) 进入 rerank(与旧逻辑一致)
        all_nodes.sort(key=lambda n: n.score or 0, reverse=True)
        all_nodes = all_nodes[:top_k * len(targets)]

        rerank = self._get_rerank(rerank_top_n)
        reranked = rerank.postprocess_nodes(all_nodes, QueryBundle(query))

        results = []
        for node in reranked:
            m = node.node.metadata
            results.append({
                "score": round(node.score or 0, 4),
                "book": m.get("book", m.get("_collection", "")),
                "part": m.get("part", ""),
                "chapter": m.get("chapter", ""),
                "section": m.get("section", ""),
                "text": node.node.text.strip(),
            })
        return results

    def _search_legacy(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        rerank_top_n: int = DEFAULT_RERANK_N,
        collection: Optional[str] = None,
    ) -> List[Dict]:
        """旧检索逻辑(llama_index 每 collection 远程 embedding)——快速路径失败时兜底。

        Args:
            query:          查询文本
            top_k:          每个 collection 的初检索召回数
            rerank_top_n:   跨 collection 合并后重排序保留数
            collection:     限定某个 collection（不传则跨所有注册 collection）

        Returns:
            [{score, book, part, chapter, section, text}, ...]
        """
        targets = [collection] if collection else self._collections

        # 多 collection 分别检索
        all_nodes = []
        for cname in targets:
            index = self._get_index(cname)
            retriever = index.as_retriever(similarity_top_k=top_k)
            nodes = retriever.retrieve(query)
            # 给 node 打上 collection 标记
            for n in nodes:
                n.node.metadata["_collection"] = cname
            all_nodes.extend(nodes)

        if not all_nodes:
            return []

        # 按相似度排序取 top_k * len(collections) 进入 rerank
        all_nodes.sort(key=lambda n: n.score or 0, reverse=True)
        all_nodes = all_nodes[:top_k * len(targets)]

        # 重排序
        rerank = self._get_rerank(rerank_top_n)
        query_bundle = QueryBundle(query)
        reranked = rerank.postprocess_nodes(all_nodes, query_bundle)

        results = []
        for node in reranked:
            m = node.node.metadata
            results.append({
                "score": round(node.score or 0, 4),
                "book": m.get("book", m.get("_collection", "")),
                "part": m.get("part", ""),
                "chapter": m.get("chapter", ""),
                "section": m.get("section", ""),
                "text": node.node.text.strip(),
            })
        return results

    def search_formatted(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        rerank_top_n: int = DEFAULT_RERANK_N,
        collection: Optional[str] = None,
    ) -> str:
        """
        检索并返回格式化文本，可直接拼入 LLM prompt。
        """
        results = self.search(
            query, top_k=top_k, rerank_top_n=rerank_top_n, collection=collection,
        )
        if not results:
            return "（未找到相关内容）"

        lines = []
        for i, r in enumerate(results, 1):
            source = f"{r['book']} > {r['part']} > {r['chapter']} > {r['section']}"
            source = source.strip(" >")
            lines.append(
                f"[来源 #{i}: {source}] (score={r['score']})\n{r['text']}\n"
            )
        return "\n".join(lines)


# ═══════════════════ CLI ═══════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python retriever.py [--chroma <路径>] [--collection <名称>] <查询文本>")
        print("  --chroma       ChromaDB 路径，默认 ./chroma_db，真题用 ./chroma_exam_db")
        print("  --collection   限定某个 collection")
        sys.exit(1)

    query_parts = []
    col_name = None
    chroma_path = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--collection" and i + 1 < len(args):
            col_name = args[i + 1]
            i += 2
        elif args[i] == "--chroma" and i + 1 < len(args):
            chroma_path = args[i + 1]
            i += 2
        else:
            query_parts.append(args[i])
            i += 1

    r = Retriever(chroma_path=chroma_path)
    print(f"📚 collections: {r.collections}")
    print(r.search_formatted(" ".join(query_parts), collection=col_name))
