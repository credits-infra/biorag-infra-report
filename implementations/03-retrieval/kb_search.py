#!/usr/bin/env python3
"""
kb_search.py — 参数化知识库检索工具(结构化 JSON 输出)

用法:
  ./.venv/bin/python kb_search.py "查询词" [--collection NAME]... [--top-k N] [--rerank N] [--chroma PATH] [--with-images]

示例:
  ./.venv/bin/python kb_search.py "什么是操纵子" --collection General_Biology --top-k 3
  ./.venv/bin/python kb_search.py "DNA复制 半保留"  # 跨全部库
  ./.venv/bin/python kb_search.py "r对策 K对策" --with-images  # 附带解析后的图片绝对路径
  ./.venv/bin/python kb_search.py "光周期现象考过哪些" --chroma ./chroma_exam_db  # 真题库检索

输出 JSON:
{
  "query": "...",
  "collections": [...],
  "count": N,
  "results": [
    {
      "chunk_id": "<collection前16字符>_<sha1-12位>",
      "collection": "General_Biology",
      "book": "General_Biology",
      "chapter": "...", "section": "...",
      "score": 0.81,
      "text": "完整 chunk 原文",
      "images": ["/abs/path/img1.jpg", ...]   # 仅 --with-images 时
    }, ...
  ]
}

设计要点:
- argparse 参数化,查询词作为单个 argv 传入,无 shell 拼接 → 无注入风险
- chunk_id 由 collection+chapter+section+text 哈希生成,稳定可校验
- JSON 输出,leaf subagent / 控制 Agent 直接解析,无需从文本猜结构
- --with-images:解析 chunk 文本中的 ![](images/xxx.jpg) 引用 → 绝对路径(MinerU 图片存放在各分册 images/ 目录,文件名全局唯一哈希)
"""
import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 全局图片索引:文件名(哈希) -> 绝对路径。构建一次,进程内复用。
_IMG_INDEX = None


def _build_image_index(docs_dir=None):
    """扫描 docs/**/images/* 建立 {文件名: 绝对路径} 映射。"""
    global _IMG_INDEX
    if _IMG_INDEX is not None:
        return _IMG_INDEX
    docs_dir = docs_dir or os.path.join(HERE, "docs")
    index = {}
    for root, _dirs, files in os.walk(docs_dir):
        for f in files:
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
                index[f] = os.path.abspath(os.path.join(root, f))
    _IMG_INDEX = index
    return index


_IMG_RE = re.compile(r"!\[[^\]]*\]\(images/([^)]+)\)")


def extract_images(text, img_index=None):
    """从 chunk 文本提取 ![](images/xxx) 引用的图片绝对路径。"""
    img_index = img_index if img_index is not None else _build_image_index()
    paths = []
    for name in _IMG_RE.findall(text):
        if name in img_index:
            paths.append(img_index[name])
    return paths


def make_chunk_id(collection: str, chapter: str, section: str, text: str) -> str:
    """稳定 chunk_id:collection 前缀 + 内容哈希(相同内容永远相同 id)。"""
    payload = f"{collection}|{chapter}|{section}|{text[:200]}"
    h = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    prefix = collection[:16].replace(" ", "_")
    return f"{prefix}_{h}"


def main() -> int:
    p = argparse.ArgumentParser(description="KB search with JSON output")
    p.add_argument("query", help="检索词")
    p.add_argument("--collection", action="append", default=None,
                   help="限定 collection 名(可多次传;不传则跨全部库)")
    p.add_argument("--top-k", type=int, default=10, help="初检索召回数(默认10)")
    p.add_argument("--rerank", type=int, default=5, help="rerank 保留数(默认5)")
    p.add_argument("--with-images", action="store_true",
                   help="解析 chunk 内图片引用,输出绝对路径")
    p.add_argument("--chroma", default=None,
                   help="ChromaDB 路径(默认 ./chroma_db 课本库;真题库传 ./chroma_exam_db)")
    args = p.parse_args()

    from retriever import Retriever  # noqa: E402  (需在 chroma 目录下)

    if args.collection:
        r = Retriever(*args.collection, chroma_path=args.chroma)
    else:
        r = Retriever(chroma_path=args.chroma)
    results = r.search(args.query, top_k=args.top_k, rerank_top_n=args.rerank)

    img_index = _build_image_index() if args.with_images else None
    out = []
    for res in results:
        cid = make_chunk_id(
            res.get("book", ""),
            res.get("chapter", ""),
            res.get("section", ""),
            res.get("text", ""),
        )
        item = {
            "chunk_id": cid,
            "collection": res.get("book", ""),
            "book": res.get("book", ""),
            "chapter": res.get("chapter", ""),
            "section": res.get("section", ""),
            "score": res.get("score", 0),
            "text": res.get("text", ""),
        }
        if args.with_images:
            item["images"] = extract_images(res.get("text", ""), img_index)
        out.append(item)

    print(json.dumps({
        "query": args.query,
        "collections": r.collections,
        "count": len(out),
        "results": out,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
