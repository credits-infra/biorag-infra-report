#!/usr/bin/env python3
"""
RAG 向量库入库脚本
========================
支持多本书独立 collection，按 Markdown 标题层级分块。

用法:
    export DASHSCOPE_API_KEY="在此填你自己的 DashScope API Key"
    python ingest.py --book "general_biology" --docs ./docs --output ./chroma_db

    # collection 名自动生成为: general_biology

环境变量:
    DASHSCOPE_API_KEY   阿里百炼 API Key（必需）
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import List

from llama_index.core import Document
from llama_index.core.ingestion import IngestionPipeline
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.dashscope import DashScopeEmbedding
from llama_index.vector_stores.chroma import ChromaVectorStore
import chromadb

from ensure_persist import force_persist

# ═══════════════════ 默认配置 ═══════════════════

HERE = Path(__file__).resolve().parent
EMBED_MODEL = "qwen3.7-text-embedding"
MAX_CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200


# ═══════════════════ 分块逻辑 ═══════════════════

def parse_full_md(md_text: str, book_name: str, part_name: str) -> List[Document]:
    """按 ## 二级标题切分 Markdown。"""
    documents: List[Document] = []
    lines = md_text.split("\n")

    current_chapter = ""
    current_section = ""
    section_lines: List[str] = []

    def flush():
        nonlocal section_lines
        if not section_lines:
            return
        raw = "\n".join(section_lines).strip()
        if raw:
            documents.append(Document(
                text=raw,
                metadata={
                    "book": book_name,
                    "part": part_name,
                    "chapter": current_chapter,
                    "section": current_section,
                },
            ))
        section_lines.clear()

    for line in lines:
        s = line.strip()
        if s.startswith("## ") and not s.startswith("### "):
            flush()
            current_section = s[3:]
            section_lines = [line]
            continue
        if s.startswith("# ") and not s.startswith("## "):
            flush()
            current_chapter = s[2:]
            current_section = ""
            section_lines = [line]
            continue
        section_lines.append(line)

    flush()
    return documents


def split_long_docs(documents: List[Document]) -> List[Document]:
    """超长 chunk 兜底切分。"""
    splitter = SentenceSplitter(
        chunk_size=MAX_CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        paragraph_separator="\n\n",
    )
    result: List[Document] = []
    for doc in documents:
        if len(doc.text) <= MAX_CHUNK_SIZE * 1.3:
            result.append(doc)
        else:
            for i, sub in enumerate(splitter.split_text(doc.text)):
                result.append(Document(
                    text=sub,
                    metadata={**doc.metadata, "chunk_index": i},
                ))
    return result


def extract_part_name(dir_name: str) -> str | None:
    """从 MinerU 目录名提取部分名。依次尝试以下格式：

    1. 第X部分 / 绪论 / 名词索引
       普通生物学-...-第一部分-细胞.pdf-xxx  → "第一部分-细胞"
    2. 上册/下册 + 章节范围
       生物化学_上册_1-4.pdf-xxx            → "上册 第1-4章"
    3. 纯章节范围
       微生物学_1-7.pdf-xxx                 → "第1-7章"
       现代分子生物学_4-8.pdf-xxx           → "第4-8章"
    """
    # 格式1: 第X部分 / 绪论 / 名词索引
    m = re.search(r"-(第[一二三四五六七八]+部分[^.]*|绪论|名词索引)", dir_name)
    if m:
        return m.group(1)

    # 格式2: _上册/下册_范围
    m = re.search(r"_([上下]册)_(\d+-\d+)", dir_name)
    if m:
        return f"{m.group(1)} 第{m.group(2)}章"

    # 格式3: _数字-数字（纯章节范围，可能带 .pdf 后缀也可能不带）
    m = re.search(r"_(\d+)-(\d+)(?:\.pdf|$)", dir_name)
    if m:
        return f"第{m.group(1)}-{m.group(2)}章"

    # 兜底: 提取 .pdf 之前的内容作为 part 名
    # "612 生物化学与分子生物学.pdf-UUID" → "612 生物化学与分子生物学"
    m = re.search(r"(.+?)\.pdf", dir_name)
    if m:
        return m.group(1)

    return None


# ═══════════════════ 主流程 ═══════════════════

def main():
    parser = argparse.ArgumentParser(description="RAG 向量库入库")
    parser.add_argument("--book", required=True, help="书名（如 '普通生物学'）")
    parser.add_argument("--collection", default=None,
                        help="ChromaDB collection 名（默认用书名拼音）")
    parser.add_argument("--docs", default=str(HERE / "docs"),
                        help="MinerU 提取结果目录")
    parser.add_argument("--output", default=str(HERE / "chroma_db"),
                        help="ChromaDB 持久化路径")
    parser.add_argument("--skip", nargs="*", default=["名词索引"],
                        help="跳过的部分（默认跳名词索引）")
    args = parser.parse_args()

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print("❌ 请 export DASHSCOPE_API_KEY='在此填你自己的 DashScope API Key'")
        sys.exit(1)

    collection_name = args.collection or args.book
    docs_dir = Path(args.docs)
    output_dir = Path(args.output)

    # ---- 1. 读取所有 full.md ----
    all_docs: List[Document] = []
    for d in sorted(docs_dir.iterdir()):
        if not d.is_dir():
            continue
        part = extract_part_name(d.name)
        if part is None or part in args.skip:
            print(f"  ⏭ 跳过: {d.name}")
            continue
        md = d / "full.md"
        if not md.exists():
            print(f"  ⚠ 缺 full.md: {d}")
            continue

        docs = parse_full_md(md.read_text(encoding="utf-8"), args.book, part)
        print(f"  ✅ {part}: {len(docs)} 个章节块")
        all_docs.extend(docs)

    if not all_docs:
        print("❌ 未找到任何文档，请检查 --docs 路径")
        sys.exit(1)

    print(f"\n📚 共 {len(all_docs)} 个二级标题块")

    # ---- 2. 兜底切分 ----
    all_docs = split_long_docs(all_docs)
    print(f"📦 切分后 {len(all_docs)} 个 chunk")

    # 预览
    for doc in all_docs[:5]:
        m = doc.metadata
        print(f"   [{m['part']}] {m['chapter']} → {m['section']}  ({len(doc.text)}字)")

    # ---- 3. Embedding ----
    embed_model = DashScopeEmbedding(
        model_name=EMBED_MODEL,
        api_key=api_key,
        embed_batch_size=10,
    )

    # ---- 4. ChromaDB ----
    output_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(output_dir))

    try:
        client.delete_collection(collection_name)
        print(f"\n🗑 已清除旧 collection: {collection_name}")
    except Exception:
        pass

    col = client.create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )
    vector_store = ChromaVectorStore(chroma_collection=col)

    # ---- 5. 入库 ----
    print(f"\n🚀 开始向量化: {args.book} → {collection_name}")
    print(f"   模型: {EMBED_MODEL} | chunks: {len(all_docs)}\n")

    pipeline = IngestionPipeline(
        transformations=[embed_model],
        vector_store=vector_store,
    )

    start = time.time()
    total = len(all_docs)
    batch = 50
    for i in range(0, total, batch):
        chunk = all_docs[i : i + batch]
        pipeline.run(documents=chunk)
        done = min(i + batch, total)
        t = time.time() - start
        print(f"  [{done}/{total}] {done/total*100:.0f}%  {t:.0f}s  {done/t:.1f}条/s")
        time.sleep(0.3)

    t = time.time() - start
    print(f"\n✅ 完成: {args.book} | {total} chunks / {t:.0f}s")
    print(f"   collection: {collection_name}")
    print(f"   ChromaDB  : {output_dir.resolve()}")

    # ---- 6. 落盘保障(chroma 1.5.9 延迟持久化 workaround)----
    # HNSW 索引默认 sync_threshold=1000,不足时只存 WAL;幂等 re-upsert 触发 _persist()
    print(f"\n💾 触发落盘(ensure_persist)...")
    force_persist(client, collection_name, str(output_dir), verbose=True)


if __name__ == "__main__":
    main()
