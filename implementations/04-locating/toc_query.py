#!/usr/bin/env python3
"""toc_query.py — 用一维有序区间树回答「这一行或这一块属于书的哪一章哪一节」。

这份实现是为技术报告附带的参考实现,不是生产脚本原文(生产脚本未保留)。
它只读 toc_index.json 与 chunk_loc.json 两个数据文件,不碰 ChromaDB。

用法:
    python3 toc_query.py --index /path/to/toc_index chunk Principle_of_Biochem_9f3a1c0b77d2
    python3 toc_query.py --index /path/to/toc_index line 生物化学原理 5021
    python3 toc_query.py --index /path/to/toc_index tree 生物化学原理

数据格式见同目录 README.md。
"""
from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_right
from pathlib import Path

# 引用出处用的标题类型:篇/章/节/小节。其余(栏目、图注、题型)不参与出处。
CHAIN_KINDS = ("篇", "章", "节", "小节", "小点")


def load(index_dir: Path):
    toc_path = index_dir / "toc_index.json"
    loc_path = index_dir / "chunk_loc.json"
    if not toc_path.exists():
        sys.exit(f"找不到 {toc_path}")
    toc = json.loads(toc_path.read_text(encoding="utf-8"))
    loc = {}
    if loc_path.exists():
        loc = json.loads(loc_path.read_text(encoding="utf-8"))
    return toc, loc


def starts_of(doc_entry: dict) -> list[int]:
    """取一本书的节点起始行号数组。数据里若有 starts 字段就直接用,否则现算。"""
    if doc_entry.get("starts"):
        return doc_entry["starts"]
    return [n["start"] for n in doc_entry["nodes"]]


def chain_at(doc_entry: dict, line: int) -> list[dict]:
    """返回包含该行的标题链,从篇到节。

    区间定义保证:每个节点的 end 是下一个同级或更高级标题的前一行,
    所以 bisect_right(starts, line) - 1 得到的节点必然包含这一行。
    """
    starts = starts_of(doc_entry)
    nodes = doc_entry["nodes"]
    i = bisect_right(starts, line) - 1
    if i < 0:
        return []
    chain = []
    while i is not None and i >= 0:
        node = nodes[i]
        if node["start"] <= line <= node.get("end", line):
            chain.append(node)
        parent = node.get("parent")
        i = parent if isinstance(parent, int) else None
    chain.reverse()
    return [n for n in chain if n.get("kind") in CHAIN_KINDS]


def fmt(chain: list[dict]) -> str:
    if not chain:
        return "(没有归属的层级)"
    return " > ".join(f'{n["text"]}' for n in chain)


def cmd_chunk(args, toc, loc):
    if args.chunk_id not in loc:
        sys.exit(f"chunk_loc.json 里没有 {args.chunk_id}")
    rec = loc[args.chunk_id]
    print(json.dumps({
        "chunk_id": rec["chunk_id"],
        "collection": rec.get("collection"),
        "book": rec.get("book"),
        "doc": rec.get("doc"),
        "line": rec.get("line"),
        "chain": [n["text"] for n in rec.get("chain") or []],
    }, ensure_ascii=False, indent=2))


def cmd_line(args, toc, loc):
    if args.book not in toc:
        sys.exit(f"toc_index.json 里没有这本书:{args.book};可选:{', '.join(sorted(toc))}")
    best = None
    for entry in toc[args.book]:
        if 1 <= args.line <= entry.get("n_lines", args.line):
            best = entry
            break
    if best is None:
        sys.exit(f"{args.book} 没有覆盖第 {args.line} 行的文档")
    chain = chain_at(best, args.line)
    print(f'{args.book} 第 {args.line} 行')
    print(f"  出处:{fmt(chain)}")
    print(f"  文档:{best.get('doc')}")


def cmd_tree(args, toc, loc):
    if args.book not in toc:
        sys.exit(f"toc_index.json 里没有这本书:{args.book}")
    for entry in toc[args.book]:
        print(f'--- {Path(entry.get("doc", "")).parent.name}  ({entry.get("n_lines")} 行)')
        for node in entry["nodes"]:
            if node.get("kind") in ("篇", "章"):
                pad = "  " if node.get("kind") == "章" else ""
                print(f'{pad}{node["text"]}  [{node["start"]}-{node.get("end")}]')


def main():
    ap = argparse.ArgumentParser(description="层级索引点查询")
    ap.add_argument("--index", default=".", help="toc_index.json 与 chunk_loc.json 所在目录")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("chunk", help="按 chunk_id 查行号与出处")
    p1.add_argument("chunk_id")
    p1.set_defaults(func=cmd_chunk)

    p2 = sub.add_parser("line", help="按行号查出处")
    p2.add_argument("book")
    p2.add_argument("line", type=int)
    p2.set_defaults(func=cmd_line)

    p3 = sub.add_parser("tree", help="列出某书的篇与章")
    p3.add_argument("book")
    p3.set_defaults(func=cmd_tree)

    args = ap.parse_args()
    toc, loc = load(Path(args.index))
    args.func(args, toc, loc)


if __name__ == "__main__":
    main()
