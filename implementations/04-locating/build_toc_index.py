#!/usr/bin/env python3
"""build_toc_index.py — 从被拍平的 Markdown 重建教材层级，并把每个向量块定位回原文行号。

背景：PDF 转 Markdown 时 h1 到 h4 被压成同一个级别，入库脚本「`# ` 是章、`## ` 是节」的假设
落空，chroma 里的 `chapter` 大面积错位。重建不改数据库，只产出两个离线文件：

    toc_index.json  每本书的标题节点数组（有序区间树）与起始行号数组
    chunk_loc.json  chunk_id → 原文行号、完整标题链、原库 chapter（便于对照）

检索侧用 bisect 做点查询，O(log n)；本脚本负责生成，只读 chroma，不写一个字节。

用法：
    python3 build_toc_index.py --docs /srv/bio-kb/docs --chroma /srv/bio-kb/chroma_db \
        --book-map book_map.json --out /srv/bio-kb/toc_index

    python3 build_toc_index.py ... --dry-run       # 只统计，不写文件

输出格式与 toc_query.py 保持兼容，字段含义见同目录 README.md。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

# ─────────────────────────── 分类规则 ───────────────────────────
# 规则来自实际数据的归纳：先把「栏目」摘出去（专栏名、题型、图注、卷头），
# 再按篇/章/节/小节/小点分层。顺序不能换——栏目优先，否则「参考文献」会被当成节。

LEVEL_篇 = 1
LEVEL_章 = 1
LEVEL_节 = 2
LEVEL_小节 = 3
LEVEL_小点 = 4
LEVEL_栏目 = 5
LEVEL_未识别 = 6

KIND_篇 = "章"  # 与既有数据保持一致：篇与章同为 level 1，kind 统一记作「章」
KIND_章 = "章"
KIND_节 = "节"
KIND_小节 = "小节"
KIND_小点 = "小点"
KIND_栏目 = "栏目"
KIND_未识别 = "未识别"

# 栏目：不计入层级，但保留为节点（子节点仍挂在它们下面，便于回溯原文）
栏目词 = (
    "关键概念|综述文献|研究论文文献|现实案例|科学故事|考生须知|"
    "考试要求|考试内容|考试形式|试卷结构|考试科目基本要求及适用范围概述|"
    "习题|思考题|参考文献|主要参考书目|网上资源|更多网络学习资源|问题|"
    "提要|摘要|目录|序言|前言|绪论|附录|索引|词汇表|小结|知识窗|"
    "阅读材料|专栏|知识链接|知识拓展|本章小结|复习题"
)
RE_栏目 = re.compile(rf"^(?:{栏目词})")
RE_栏目_卷头 = re.compile(r"^(?:中科院|中国科学院大学|《)")
RE_栏目_题型 = re.compile(r"^[0-9一二三四五六七八九十]+[.．、]\s*[（(]?\s*(?:是非题|填空|选择|简答|问答|名词解释|判断|计算|论述)")
RE_栏目_图注 = re.compile(r"^(?:[←→]?\s*)?(?:图|表|Box|框|例)\s*[0-9]")
RE_栏目_字母 = re.compile(r"^[A-Z]$")

RE_篇 = re.compile(r"^第\s*[〇零一二三四五六七八九十百千0-9]+\s*篇")
RE_部分 = re.compile(r"^第\s*[〇零一二三四五六七八九十百千0-9]+\s*部分")
RE_章 = re.compile(r"^第\s*[〇零一二三四五六七八九十百千0-9]+\s*章")
RE_纯章号 = re.compile(r"^第\s*[〇零一二三四五六七八九十百千0-9]+\s*章$")
RE_节 = re.compile(r"^第\s*[〇零一二三四五六七八九十百千0-9]+\s*节")
RE_中文序号 = re.compile(r"^[〇零一二三四五六七八九十]+\s*[、.]")
RE_多级数字 = re.compile(r"^(\d+(?:\.\d+)+)\s*")
RE_罗马章 = re.compile(r"^(?:[IVX]{1,5})\s+\S")
RE_裸章号 = re.compile(r"^\d{1,2}\s+\S")  # 普通生物学：「15 原核生物多样性」
RE_小节 = re.compile(r"^(?:[（(][〇零一二三四五六七八九十0-9]+[）)]|\d+[）)])\s*")
RE_小点 = re.compile(r"^(?:[①-⑳]|\d+[.．]\s*\S)")
RE_标题行 = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass
class Node:
    text: str
    kind: str
    level: int
    start: int
    end: int = 0
    parent: int | None = None

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "kind": self.kind,
            "level": self.level,
            "start": self.start,
            "end": self.end,
            "parent": self.parent,
        }


def classify(text: str, heading_level: int) -> tuple[str, int]:
    """把一个标题行判定为层级种类。找不到依据时返回未识别。"""
    t = text.strip()
    if not t:
        return KIND_未识别, LEVEL_未识别
    if (
        RE_栏目.match(t)
        or RE_栏目_题型.match(t)
        or RE_栏目_图注.match(t)
        or RE_栏目_字母.match(t)
    ):
        return KIND_栏目, LEVEL_栏目
    # 真题卷头与大纲首行是 level 1 的容器，把它当章才能引用「某年某题型」
    if RE_栏目_卷头.match(t) and heading_level >= 2:
        return KIND_栏目, LEVEL_栏目
    if RE_篇.match(t) or RE_部分.match(t):
        return KIND_篇, LEVEL_篇
    if RE_章.match(t):
        return KIND_章, LEVEL_章
    if RE_节.match(t):
        return KIND_节, LEVEL_节
    if RE_中文序号.match(t):
        # 中文顿号序号在教材里当节用；在真题里当大题用（已被题型规则拦下）
        return KIND_节, LEVEL_节
    m = RE_多级数字.match(t)
    if m:
        depth = m.group(1).count(".") + 1
        if depth >= 3:
            return KIND_小节, LEVEL_小节
        return KIND_节, LEVEL_节
    if RE_小节.match(t):
        return KIND_小节, LEVEL_小节
    if RE_小点.match(t):
        return KIND_小点, LEVEL_小点
    # 一级标题在教材里就是章（含「第一篇 X」「VII X」「15 X」这类无「章」字的写法）
    if heading_level == 1:
        return KIND_章, LEVEL_章
    if RE_裸章号.match(t) or RE_罗马章.match(t):
        return KIND_章, LEVEL_章
    return KIND_未识别, LEVEL_未识别


def extract_headings(md_text: str) -> list[tuple[int, str, int]]:
    """抽标题行，返回 (行号, 文本, 标题级别)。行号从 1 开始，与编辑器一致。"""
    out: list[tuple[int, str, int]] = []
    for i, line in enumerate(md_text.splitlines(), start=1):
        m = RE_标题行.match(line)
        if m:
            out.append((i, m.group(2).strip(), len(m.group(1))))
    return out


def merge_chapter_numbers(items: list[tuple[int, str, int]]) -> list[tuple[int, str, int, str, int]]:
    """把「第8章」与紧随其后的裸章名合并成一条。

    教材排版常见两行式：`# 第8章` 换行 `# 微生物的遗传`。不合并的话，章名丢一半，
    而且章号节点会把后面所有节都挂在自己名下。返回 (行号, 文本, 级别, kind, level)。
    """
    out: list[tuple[int, str, int, str, int]] = []
    pending: tuple[int, str, int] | None = None
    for line_no, text, hlevel in items:
        if RE_纯章号.match(text):
            if pending is not None:
                kind, lv = classify(pending[1], pending[2])
                out.append((pending[0], pending[1], pending[2], kind, lv))
            pending = (line_no, text, hlevel)
            continue
        if pending is not None:
            kind, lv = classify(text, hlevel)
            if kind == KIND_未识别 or (kind == KIND_章 and not RE_章.match(text)):
                # 裸章名：与挂起的章号合并
                merged = f"{pending[1]} {text}"
                out.append((pending[0], merged, pending[2], KIND_章, LEVEL_章))
                pending = None
                continue
            out.append((pending[0], pending[1], pending[2], KIND_章, LEVEL_章))
            pending = None
        kind, lv = classify(text, hlevel)
        out.append((line_no, text, hlevel, kind, lv))
    if pending is not None:
        out.append((pending[0], pending[1], pending[2], KIND_章, LEVEL_章))
    return out


def build_tree(items: Sequence[tuple[int, str, int, str, int]], n_lines: int) -> list[Node]:
    """用栈建有序区间树。每个节点的 end 是下一个同级或更高级标题的前一行。"""
    nodes: list[Node] = []
    stack: list[int] = []
    for line_no, text, _hlevel, kind, level in items:
        while stack and nodes[stack[-1]].level >= level:
            nodes[stack.pop()].end = line_no - 1
        parent = stack[-1] if stack else None
        nodes.append(Node(text=text, kind=kind, level=level, start=line_no, parent=parent))
        stack.append(len(nodes) - 1)
    last = n_lines if n_lines > 0 else 1
    for idx in stack:
        nodes[idx].end = last
    for node in nodes:
        if node.end < node.start:
            node.end = node.start
    return nodes


def _attr(node, name):
    """节点既可能是 Node 也可能是 toc_index.json 里的 dict，统一取值。"""
    if isinstance(node, dict):
        return node[name]
    return getattr(node, name)


def chain_at(nodes: Sequence, starts: Sequence[int], line: int) -> list:
    """点查询：行号落在哪个节点区间，沿 parent 上溯得完整链。"""
    i = bisect_right(list(starts), line) - 1
    if i < 0:
        return []
    out: list = []
    while i is not None and i >= 0:
        node = nodes[i]
        if _attr(node, "start") <= line <= _attr(node, "end"):
            out.append(node)
        parent = _attr(node, "parent")
        i = parent if isinstance(parent, int) else -1
    out.reverse()
    return out


def index_book(md_text: str) -> dict:
    """把一本书的 Markdown 编成索引条目。"""
    n_lines = md_text.count("\n") + (0 if md_text.endswith("\n") else 1)
    items = merge_chapter_numbers(extract_headings(md_text))
    nodes = build_tree(items, n_lines)
    return {
        "n_lines": n_lines,
        "nodes": nodes,
        "starts": [n.start for n in nodes],
    }


# ─────────────────────────── 块定位 ───────────────────────────
def locate_chunk(full_text: str, chunk_text: str) -> int | None:
    """在原文里定位块的起始行号。

    用块自身的正文前两行做锚点，而不是拿标题文本查表：重复标题（「小结」出现十几次）
    会把所有块指到第一次出现的位置。锚点找不到时退化为首行前 120 字。
    """
    lines = [x for x in chunk_text.splitlines() if x.strip()]
    if not lines:
        return None
    # 锚点从长到短试：能匹配上的最长锚点最不容易歧义（同一段文字可能在多份分册里出现），
    # 匹配不上再逐级放宽，最后退到单行，保证短块也能定位。
    candidates = ["\n".join(lines[:4])[:260], "\n".join(lines[:3])[:220], "\n".join(lines[:2])[:180],
                  lines[0][:120], lines[0][:60]]
    for anchor in candidates:
        if not anchor:
            continue
        pos = full_text.find(anchor)
        if pos >= 0:
            return full_text.count("\n", 0, pos) + 1
    return None


def make_chunk_id(collection: str, chapter: str, section: str, text: str) -> str:
    """与检索工具同源的稳定 id：collection 前 16 字符 + 内容哈希 12 位。

    公式必须与 kb_search.make_chunk_id 完全一致，否则 loc 查不到检索返回的 id。
    """
    payload = f"{collection}|{chapter}|{section}|{text[:200]}"
    h = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    prefix = collection[:16].replace(" ", "_")
    return f"{prefix}_{h}"


@dataclass
class ChunkRecord:
    collection: str
    text: str
    metadata: dict
    uuid: str = ""


def build_chunk_loc(
    records: Iterable[ChunkRecord],
    toc: dict,
    book_of_collection: dict[str, str],
) -> tuple[dict, dict]:
    """把每个块定位到「书名 + 文档 + 行号 + 标题链」。

    返回 (chunk_loc, 统计)。同一个 chunk_id 可能对应多个块（原设计固有碰撞），
    保留第一次定位，并计入统计。
    """
    full_cache: dict[str, str] = {}
    out: dict[str, dict] = {}
    stats = {"total": 0, "located": 0, "missing_anchor": 0, "no_book": 0,
             "collisions": 0, "ambiguous": 0}
    for rec in records:
        stats["total"] += 1
        meta = rec.metadata or {}
        # 元数据里的 book 常等于 collection 名，不能直接当目录名；显式/推导的映射优先
        book = book_of_collection.get(rec.collection) or meta.get("book")
        cid = make_chunk_id(
            rec.collection, meta.get("chapter", "") or "", meta.get("section", "") or "", rec.text
        )
        if cid in out:
            stats["collisions"] += 1
            continue
        if not book or book not in toc:
            stats["no_book"] += 1
            continue
        located = None
        ambiguous = False
        for entry in sorted(toc[book], key=lambda e: e["doc"]):
            full = full_cache.get(entry["doc"])
            if full is None:
                try:
                    full = Path(entry["doc"]).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                full_cache[entry["doc"]] = full
            line = locate_chunk(full, rec.text)
            if line is None:
                continue
            # 分册页码范围重叠、或者「习题／参考书目」这类栏目在每册都出现时，同一段文字会在
            # 两份以上文档里命中。取排序后的第一份保证确定性；歧义按【块】计数，一块只记一次。
            if located is not None:
                ambiguous = True
                continue
            chain = chain_at(entry["nodes"], entry["starts"], line)
            located = {
                "chunk_id": cid,
                "uuid": rec.uuid,
                "collection": rec.collection,
                "book": book,
                "doc": Path(entry["doc"]).parent.name,
                "line": line,
                "chain": [{"text": _attr(n, "text"), "kind": _attr(n, "kind"), "level": _attr(n, "level")} for n in chain],
                "old_chapter": meta.get("chapter", "") or "",
            }
            # 不 break：继续扫完其余分册，把「同一段文字在多份文档里都命中」计入歧义
        if located is None:
            stats["missing_anchor"] += 1
            continue
        if ambiguous:
            stats["ambiguous"] += 1
        out[cid] = located
        stats["located"] += 1
    return out, stats


def resolve_book_map(collections: Sequence[str], toc_books: Sequence[str],
                     explicit: dict[str, str],
                     meta_books: dict[str, str] | None = None) -> tuple[dict[str, str], list[str]]:
    """决定每个 collection 对应哪本书的目录。

    优先级：显式映射 > 元数据里的 book（常见情形是它等于 collection 名）> 归一化后的互相包含。
    剩下对不上的库要报出来——静默跳过会让覆盖率看起来像 0 却不说明原因。
    """
    meta_books = meta_books or {}
    out: dict[str, str] = {}
    for col in collections:
        if col in explicit:
            out[col] = explicit[col]
            continue
        mb = meta_books.get(col, "")
        if mb in toc_books:
            out[col] = mb
            continue
        norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", col.lower())
        for book in toc_books:
            nb = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", book.lower())
            if nb and (nb in norm or norm in nb):
                out[col] = book
                break
    unmapped = [c for c in dict.fromkeys(collections) if c not in out]
    return out, unmapped


# ─────────────────────────── chroma 读取（仅 CLI） ───────────────────────────
def iter_chroma_records(chroma_path: str, collections: Sequence[str] | None) -> Iterator[ChunkRecord]:
    import chromadb  # 延迟导入：单元测试不需要 chromadb

    client = chromadb.PersistentClient(path=chroma_path)
    names = [c.name for c in client.list_collections()]
    if collections:
        names = [n for n in names if n in set(collections)]
    for name in sorted(names):
        col = client.get_collection(name)
        got = col.get(include=["documents", "metadatas"])
        ids = got.get("ids") or []
        docs = got.get("documents") or []
        metas = got.get("metadatas") or []
        for i, doc in enumerate(docs):
            yield ChunkRecord(
                collection=name,
                text=doc or "",
                metadata=dict(metas[i] or {}) if i < len(metas) else {},
                uuid=ids[i] if i < len(ids) else "",
            )


def iter_docs_books(docs_root: Path) -> Iterator[tuple[str, Path]]:
    """遍历 docs/<书名>/<分册>-<uuid>/full.md。"""
    for book_dir in sorted(p for p in docs_root.iterdir() if p.is_dir()):
        for full in sorted(book_dir.glob("*/full.md")):
            yield book_dir.name, full


# ─────────────────────────── 主流程 ───────────────────────────
def build(docs_root: Path, chroma_path: Path | None, book_map: dict[str, str],
          collections: Sequence[str] | None) -> tuple[dict, dict, dict]:
    toc: dict[str, list[dict]] = {}
    for book, full in iter_docs_books(docs_root):
        md = full.read_text(encoding="utf-8", errors="replace")
        entry = index_book(md)
        toc.setdefault(book, []).append({
            "doc": str(full),
            "n_lines": entry["n_lines"],
            "nodes": [n.as_dict() for n in entry["nodes"]],
            "starts": entry["starts"],
        })
    toc_stats = {}
    for book, docs in toc.items():
        nodes = [n for d in docs for n in d["nodes"]]
        un = sum(1 for n in nodes if n["kind"] == KIND_未识别)
        toc_stats[book] = {
            "docs": len(docs),
            "nodes": len(nodes),
            "chapters": sum(1 for n in nodes if n["kind"] == KIND_章),
            "unrecognized": un,
            "unrecognized_rate": round(un / len(nodes), 4) if nodes else 0.0,
        }
    if chroma_path is None:
        return toc, {}, toc_stats
    records = list(iter_chroma_records(str(chroma_path), collections))
    resolved, unmapped = resolve_book_map(
        [r.collection for r in records], list(toc), book_map,
        {r.collection: (r.metadata or {}).get("book", "") for r in records},
    )
    loc, loc_stats = build_chunk_loc(records, toc, resolved)
    return toc, loc, {"books": toc_stats, "chunks": loc_stats,
                      "mapped": len(resolved), "unmapped": unmapped}


def print_report(toc_stats: dict, loc_stats: dict, unmapped: Sequence[str] = ()) -> None:
    print("=== 1) 全量建树 ===")
    total_nodes = total_un = 0
    for book, s in sorted(toc_stats.items(), key=lambda kv: -kv[1]["nodes"]):
        print(f"  {book:<16} 文档 {s['docs']:3d} | 节点 {s['nodes']:5d} | 章 {s['chapters']:3d} | 未识别 {s['unrecognized']:4d} ({s['unrecognized_rate']*100:.1f}%)")
        total_nodes += s["nodes"]
        total_un += s["unrecognized"]
    if total_nodes:
        print(f"  合计 节点 {total_nodes} | 未识别 {total_un} ({total_un/total_nodes*100:.1f}%)")
    if unmapped:
        print("  以下 collection 没对上任何书目录，需要 --book-map 显式指定：")
        for name in unmapped:
            print(f"    {name}")
    if loc_stats:
        print("=== 2) 块 → 行号 ===")
        for key in ("total", "located", "missing_anchor", "no_book", "collisions", "ambiguous"):
            print(f"  {key:<14} {loc_stats.get(key, 0)}")
        if loc_stats.get("total"):
            print(f"  定位覆盖率     {loc_stats['located']/loc_stats['total']*100:.2f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description="从 Markdown 重建层级索引并定位向量块")
    ap.add_argument("--docs", required=True, help="docs 根目录（每个子目录是一本书）")
    ap.add_argument("--chroma", default="", help="ChromaDB 目录；给出时同时生成 chunk_loc.json")
    ap.add_argument("--book-map", default="", help="{collection: 书名} 的 JSON 文件，用于元数据缺 book 的库")
    ap.add_argument("--collection", action="append", default=[], help="只处理指定 collection（可重复）")
    ap.add_argument("--out", default="", help="输出目录；省略时只打印统计")
    ap.add_argument("--dry-run", action="store_true", help="不写文件")
    args = ap.parse_args()

    docs_root = Path(args.docs)
    if not docs_root.is_dir():
        print(f"docs 目录不存在：{docs_root}", file=sys.stderr)
        return 1
    book_map = {}
    if args.book_map:
        book_map = json.loads(Path(args.book_map).read_text(encoding="utf-8"))
    chroma_path = Path(args.chroma) if args.chroma else None
    toc, loc, stats = build(docs_root, chroma_path, book_map, args.collection or None)
    print_report(stats.get("books", stats), stats.get("chunks", {}), stats.get("unmapped", ()))

    if args.out and not args.dry_run:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "toc_index.json").write_text(json.dumps(toc, ensure_ascii=False), encoding="utf-8")
        if loc:
            (out / "chunk_loc.json").write_text(json.dumps(loc, ensure_ascii=False), encoding="utf-8")
        print(f"已写出：{out}/toc_index.json" + ("、chunk_loc.json" if loc else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
