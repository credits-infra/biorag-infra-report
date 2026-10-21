#!/usr/bin/env python3
"""rag_readonly.py — 只读检索 MCP server(专供 QQ profile「学习助手」)。

设计原则(硬约束,勿放宽):
    1. 只读    : 只做 chroma 检索与取块;无写、无 shell、无任意命令、无任意文件路径入参
    2. 库白名单: 课本 7 库 + 考纲 2 库 + 历年真题库(独立 chroma 路径);recite 自编手册不在内
    3. 无注入   : 子进程用 list 形式 argv + shell=False,查询词作为单个参数传递
    4. 限额     : query ≤ 500 字符;top_k ≤ 10;单块返回 ≤ 900 字符(正文截断);子进程超时 120s
    5. 路径受限 : 正文类工具只回 collection/chapter/section/chunk_id/text,不回文件系统路径;
                  **插图类工具**(get_chunk_images / get_doc_images)只回
                  `/srv/bio-knowledge/image_export/` 白名单内的绝对路径(硬链接目录,
                  resolve() 不会穿透到 docs/),用于写 `MEDIA:<path>` 发图

实现方式:
    本进程用「含 mcp 库」的解释器(mcp_venv)运行;真正的检索由子进程调用
    chroma venv 的 kb_search.py / kb_get_chunk.py 完成 —— 避免往 chroma venv 里装 mcp
    而破坏其 pydantic 依赖(那会连带打挂主 Agent 的 RAG 工具)。

⚠️ 子进程必须清空 PYTHONPATH / PYTHONHOME,否则 Hermes 的注入会让 chroma venv
   报 `ModuleNotFoundError: pydantic_core._pydantic_core`(看着像库损坏,实为环境污染)。

库清单(2026-10-13 实测枚举):
    chroma_db/      课本 7 个 + 考纲 2 个
    chroma_exam_db/ 历年真题 48~49 个 collection(612-Y1996..Y2026 / 888-Y2005..Y2026)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

CHROMA_DIR = "/srv/bio-kb"
PY = os.path.join(CHROMA_DIR, ".venv/bin/python")
KB_SEARCH = os.path.join(CHROMA_DIR, "kb_search.py")
KB_GET = os.path.join(CHROMA_DIR, "kb_get_chunk.py")

TEXTBOOK_CHROMA_PATH = os.path.join(CHROMA_DIR, "chroma_db")
EXAM_CHROMA_PATH = os.path.join(CHROMA_DIR, "chroma_exam_db")

ALLOWED_COLLECTIONS = [
    # —— 课本 7 库 ——
    "Biological_Chemistry_Zhu_Sheng_Geng",       # 朱圣庚《生物化学》第4版      2165 块
    "Principle_of_Biochemistry_Yang_RongWu",     # 杨荣武《生物化学原理》      2067 块
    "Morden_Molecular_Biology",                  # 朱玉贤《现代分子生物学》     666 块
    "Lewin-s-Gene-X-ZH",                         # Lewin《基因 X》            1993 块
    "Microbiology",                              # 微生物学                   1344 块
    "General_Biology",                           # 陈阅增《普通生物学》         1056 块
    "Genetics",                                  # 遗传学                      613 块
    # —— 考纲 2 库 ——
    "612-Biochemistry-and-Molecular-Biology-Guideline",   # 612 考纲     65 块
    "888-Comprehensive-Biology-Guideline",                # 888 考纲     17 块
]

MAX_QUERY_CHARS = 500
MAX_TOP_K = 10
DEFAULT_TOP_K = 5
PER_CHUNK_CHARS = 900     # 单条返回给模型的正文截断
SUBPROCESS_TIMEOUT = 120


def _clean_env() -> dict:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def _run(script: str, args: list[str], timeout: int = SUBPROCESS_TIMEOUT,
         extra_env: dict | None = None) -> tuple[int, str, str]:
    """跑 chroma venv 里的脚本。返回 (returncode, stdout, stderr)。never raises for rc>0.

    extra_env: 额外环境变量。切库路径必须走这里 ——
        · kb_search.py   支持 `--chroma <path>` 参数
        · kb_get_chunk.py **只认环境变量 BIO_CHROMA_PATH**,传 --chroma 会报 usage 错(实测)
    """
    env = _clean_env()
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run(
            [PY, script, *args],
            capture_output=True, text=True, timeout=timeout,
            env=env, cwd=CHROMA_DIR, shell=False,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -9, "", f"检索超时(>{timeout}s)"
    except Exception as exc:  # noqa: BLE001
        return -1, "", f"子进程启动失败: {exc}"


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _clamp_top_k(top_k) -> int:
    try:
        k = int(top_k)
    except Exception:  # noqa: BLE001
        k = DEFAULT_TOP_K
    return max(1, min(k, MAX_TOP_K))


def _validate_query(query) -> tuple[str | None, str | None]:
    """返回 (clean_query, error)。"""
    if not isinstance(query, str) or not query.strip():
        return None, "query 不能为空"
    q = query.strip()
    if len(q) > MAX_QUERY_CHARS:
        return None, f"query 过长(上限 {MAX_QUERY_CHARS} 字符)"
    return q, None


# ---------------------------------------------------------------- 层级索引(真实出处)
# 由 build_toc_index.py 生成(全程只读 chroma + 只读 md,不写库)。
# 用途:chroma 里的 chapter/section 字段在入库时就错了(标题层级被 PDF 转换拍平),
#       这里用 md 原文重建的真实层级链替代它。
TOC_INDEX_PATH = os.path.join(CHROMA_DIR, "toc_index", "chunk_loc.json")
_TOC_INDEX_CACHE: dict | None = None


def _toc_index() -> dict:
    """懒加载(首次调用读一次,5MB 左右,常驻内存)。"""
    global _TOC_INDEX_CACHE
    if _TOC_INDEX_CACHE is None:
        try:
            with open(TOC_INDEX_PATH, encoding="utf-8") as f:
                _TOC_INDEX_CACHE = json.load(f)
        except Exception:
            _TOC_INDEX_CACHE = {}
    return _TOC_INDEX_CACHE


def _real_location(chunk_id: str) -> dict | None:
    """查该块的**真实**层级链。索引缺失时返回 None(工具照常工作,只是没有 location)。"""
    v = _toc_index().get(chunk_id or "")
    if not v:
        return None
    chain = v.get("chain") or []
    if not chain:
        return None
    return {
        "path": " > ".join((x.get("text") or "") for x in chain),
        "kinds": [x.get("kind") for x in chain],
        "md_line": v.get("line"),
        "md_doc": v.get("doc"),
    }


def _shape_results(data: dict, k: int) -> list[dict]:
    out = []
    for r in (data.get("results") or [])[:k]:
        out.append({
            "chunk_id": r.get("chunk_id"),
            "collection": r.get("collection"),
            "chapter": r.get("chapter") or "",
            "section": r.get("section") or "",
            "score": r.get("score"),
            "text": (r.get("text") or "")[:PER_CHUNK_CHARS],
        })
    return out


# ---------------------------------------------------------------- MCP server
from mcp.server.mcpserver import MCPServer  # noqa: E402

server = MCPServer(
    name="rag",
    title="只读检索(课本 / 考纲 / 真题)",
    version="1.1.0",
    instructions=(
        "只读的 612/888 考研检索工具:课本原文、考试大纲、历年真题。"
        "用于回答专业课问题时取原文作依据。所有结论必须来自本工具返回的原文;"
        "检索不到就如实说没查到,不要靠通识印象作答。真题库是按年切块的零散记录"
        "(答案标记/名词列表/笔记),不是完整试卷,引用时要说明这一点。"
    ),
    log_level="WARNING",
)


@server.tool()
def search_textbooks(query: str, top_k: int = DEFAULT_TOP_K, collection: str = "") -> str:
    """在 612/888 考研课本与考纲库里检索原文片段(只读)。

    Args:
        query: 检索词。可用中文/英文/缩写,如「糖酵解 限速酶」「原核 真核 复制 比较」。
        top_k: 返回条数,1~10,默认 5。
        collection: 限定单个库(留空=跨全部 9 个课本/考纲库检索)。

    返回 JSON:{"ok":true,"count":N,"results":[{chunk_id,collection,chapter,section,score,text}]}
    用 chunk_id 可再调 get_chunk 取该块完整原文。
    """
    q, err = _validate_query(query)
    if err:
        return _j({"ok": False, "error": err})
    k = _clamp_top_k(top_k)

    cols = ALLOWED_COLLECTIONS
    if collection:
        col = collection.strip()
        if col not in ALLOWED_COLLECTIONS:
            return _j({"ok": False, "error": f"库不在白名单:{col}", "allowed": ALLOWED_COLLECTIONS})
        cols = [col]

    args = [q, "--top-k", str(k), "--rerank", str(k), "--chroma", TEXTBOOK_CHROMA_PATH]
    for c in cols:
        args += ["--collection", c]

    rc, out, err2 = _run(KB_SEARCH, args)
    if rc != 0:
        hint = ""
        if not os.environ.get("DASHSCOPE_API_KEY"):
            hint = ";环境缺少 DASHSCOPE_API_KEY,检索服务不可用"
        return _j({"ok": False, "error": f"检索失败(rc={rc}){hint}", "stderr": (err2 or "")[-400:]})
    try:
        data = json.loads(out)
    except Exception as exc:  # noqa: BLE001
        return _j({"ok": False, "error": f"检索输出解析失败:{exc}", "raw_head": out[:300]})

    results = _shape_results(data, k)
    for r in results:
        loc = _real_location(r.get("chunk_id") or "")
        if loc:
            # 优先用真实出处:引用时请用 location,不要用 chapter/section(那是入库时的错值)
            r["location"] = loc["path"]
    return _j({"ok": True, "query": q, "count": len(results), "results": results})


@server.tool()
def search_exam_papers(query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """在 612/888 历年真题库(1996-2026)中检索(只读)。

    ⚠️ 真题库是按年切块的**零散记录**(判断题答案序列、名词列表、考生笔记等),不是完整试卷。
       适合查「某年考了哪些名词/考点」;引用时须说明来源形态,不要当成原卷逐字引用。

    Args:
        query: 检索词。把年份/科目也写进 query 效果更好,如「2020 痛风 尿酸」「2013 名词 米氏常数」。
        top_k: 返回条数,1~10,默认 5。

    返回 JSON:{"ok":true,"count":N,"results":[{chunk_id,collection(如 612-Y2020-blank),score,text}]}
    """
    q, err = _validate_query(query)
    if err:
        return _j({"ok": False, "error": err})
    k = _clamp_top_k(top_k)

    # 不传 --collection → kb_search 自动发现该库下全部年份 collection
    args = [q, "--top-k", str(k), "--rerank", str(k), "--chroma", EXAM_CHROMA_PATH]

    rc, out, err2 = _run(KB_SEARCH, args)
    if rc != 0:
        hint = ""
        if not os.environ.get("DASHSCOPE_API_KEY"):
            hint = ";环境缺少 DASHSCOPE_API_KEY,检索服务不可用"
        return _j({"ok": False, "error": f"真题检索失败(rc={rc}){hint}", "stderr": (err2 or "")[-400:]})
    try:
        data = json.loads(out)
    except Exception as exc:  # noqa: BLE001
        return _j({"ok": False, "error": f"检索输出解析失败:{exc}", "raw_head": out[:300]})

    results = _shape_results(data, k)
    return _j({
        "ok": True, "query": q, "count": len(results),
        "caveat": "真题库为按年切块的零散记录(答案标记/名词列表/笔记),非完整原卷;"
                  "可作为「某年考过什么」的线索,引用时请说明来源形态。",
        "results": results,
    })


# ---------------------------------------------------------------- 目录树
# collection → 书目录名(与 build_toc_index.py 的 COL_BOOK 保持一致)
COL_BOOK = {
    "Biological_Chemistry_Zhu_Sheng_Geng": "生物化学",
    "Principle_of_Biochemistry_Yang_RongWu": "生物化学原理",
    "Morden_Molecular_Biology": "现代分子生物学",
    "Lewin-s-Gene-X-ZH": "Lewin基因X",
    "Microbiology": "微生物学",
    "General_Biology": "普通生物学",
    "Genetics": "遗传学",
    "612-Biochemistry-and-Molecular-Biology-Guideline": "612考试大纲",
    "888-Comprehensive-Biology-Guideline": "888考试大纲",
}

TOC_TREE_PATH = os.path.join(CHROMA_DIR, "toc_index", "toc_index.json")
_TOC_TREE_CACHE: dict | None = None


def _toc_tree() -> dict:
    """懒加载每本书的层级树(由 build_toc_index.py 生成)。"""
    global _TOC_TREE_CACHE
    if _TOC_TREE_CACHE is None:
        try:
            with open(TOC_TREE_PATH, encoding="utf-8") as f:
                _TOC_TREE_CACHE = json.load(f)
        except Exception:
            _TOC_TREE_CACHE = {}
    return _TOC_TREE_CACHE


@server.tool()
def get_toc(collection: str, keyword: str = "", max_items: int = 100) -> str:
    """列出某本课本/真题的**真实目录树**(章 → 节 → 小节),源自 md 原文重建。

    ⚠️ 用途:chroma 检索结果里的 chapter / section 字段在入库时就是错的
       (PDF 转 markdown 把标题层级拍平了),本工具给出的是从原文重建的正确层级。
       要确认「某个知识点在书里属于哪一章哪一节」时,用本工具。

    Args:
        collection: 库名,如 "Microbiology" / "Biological_Chemistry_Zhu_Sheng_Geng"(见 list_sources)。
        keyword:    只看标题里含此关键词的节点(留空=整个目录;建议长书务必带关键词)。
        max_items:  最多返回条数,1~200,默认 100。

    Returns:
        带层级的目录行列表,形如「第8章 微生物的遗传 > 第一节 遗传的物质基础」。
    """
    if not isinstance(collection, str) or not collection.strip():
        return _j({"ok": False, "error": "collection 不能为空", "allowed": list(COL_BOOK)})
    col = collection.strip()
    tree = _toc_tree()
    book = COL_BOOK.get(col)
    if book is None or book not in tree:
        return _j({"ok": False, "error": f"没有该库的目录索引:{col}", "allowed": list(COL_BOOK)})

    try:
        cap = max(1, min(int(max_items), 200))
    except Exception:  # noqa: BLE001
        cap = 100
    kw = (keyword or "").strip()

    lines, seen = [], 0
    for doc in tree[book]:
        nodes = doc.get("nodes") or []
        # 先算每个节点的标题链(用 parent 上溯)
        for i, n in enumerate(nodes):
            if n.get("kind") in ("栏目", "未识别"):
                continue                      # 栏目/噪声不进目录
            path, j, guard = [], i, 0
            while j is not None and j >= 0 and guard < 12:
                path.append(nodes[j].get("text") or "")
                j = nodes[j].get("parent")
                guard += 1
            path = list(reversed(path))
            if kw and not any(kw in p for p in path):
                continue
            lines.append("%s  [行%d]" % (" > ".join(path), n.get("start", 0)))
            seen += 1
            if seen >= cap:
                break
        if seen >= cap:
            break

    return _j({
        "ok": True, "collection": col, "book": book, "keyword": kw,
        "count": len(lines), "truncated": seen >= cap,
        "toc": lines,
        "note": "层级由 md 原文重建;格式「章 > 节 > 小节」。[行N] 为 md 原文行号。",
    })


@server.tool()
def get_chunk(chunk_id: str) -> str:
    """按 chunk_id 取回某个块的完整原文(只读,课本/考纲/真题通用)。

    Args:
        chunk_id: 形如「Biological_Chemi_a2bbd94547a6」「612-Y2020-blank_xxxxxxxxxxxx」
                  (由 search_textbooks / search_exam_papers 返回)。

    返回 JSON:{"ok":true,"chunk_id","collection","chapter","section","text"}
    """
    if not isinstance(chunk_id, str) or not chunk_id.strip():
        return _j({"ok": False, "error": "chunk_id 不能为空"})
    cid = chunk_id.strip()
    # 真题块的 chunk_id 形如 612-Y2020-blank_xxxxxxxxxxxx → 自动选真题库路径
    base = os.path.basename(cid)
    looks_exam = any(base.startswith(p) for p in ("612-Y", "888-Y"))
    chroma_path = EXAM_CHROMA_PATH if looks_exam else TEXTBOOK_CHROMA_PATH

    rc, out, err = _run(KB_GET, [cid], extra_env={"BIO_CHROMA_PATH": chroma_path})
    if rc == 0 and '"error"' not in out:
        return out.strip()
    # 回退:换另一个库再试一次(kb_get_chunk.py 只认 BIO_CHROMA_PATH 环境变量,不支持 --chroma)
    alt = TEXTBOOK_CHROMA_PATH if looks_exam else EXAM_CHROMA_PATH
    rc2, out2, err2 = _run(KB_GET, [cid], extra_env={"BIO_CHROMA_PATH": alt})
    if rc2 == 0 and '"error"' not in out2:
        return out2.strip()
    return _j({"ok": False, "error": f"取块失败(rc={rc})",
               "stderr": (err or "")[-300:], "detail": (out or "")[:300]})


# ---------------------------------------------------------------- 文档只读访问
# 给 QQ 侧提供「背诵手册 / 考情分析」的只读访问。这些是已经检索+总结好的产物,
# 复用它们比重新检索课本便宜得多。
# ⚠️ 安全约束(硬性):白名单目录、拒路径穿越、只读、限额、只回相对名。
#    模型侧没有任何 file 工具,必须经这三个工具才能碰到这些文件。
DOC_SOURCES = [
    ("背诵手册", "/srv/bio-knowledge/recite", "*.md"),
    ("考情分析", "/srv/bio-knowledge", "*_analysis.md"),
]
DOC_MAX_HITS = 60
DOC_MAX_LINE_CHARS = 400
DOC_MAX_READ_LINES = 400
DOC_DEFAULT_READ_LINES = 120
DOC_MAX_FILE_BYTES = 2_000_000
DOC_MAX_PATTERN_CHARS = 200


def _doc_sources() -> list[tuple]:
    """展开白名单 → [(相对名, 绝对路径, 分类)]。"""
    out = []
    for cat, root, pat in DOC_SOURCES:
        rootp = Path(root)
        if not rootp.is_dir():
            continue
        for f in sorted(rootp.glob(pat)):
            if not f.is_file() or f.suffix != ".md":
                continue
            try:
                if f.stat().st_size > DOC_MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            out.append((f.name, str(f.resolve()), cat))
    return out


def _doc_resolve(name: str):
    """把模型给的文档名解析成白名单内的绝对路径。返回 (abs_path, error)。"""
    if not isinstance(name, str) or not name.strip():
        return None, "文档名不能为空"
    n = name.strip()
    if ".." in n or "/" in n or "\\" in n or n.startswith("/"):
        return None, "只接受纯文件名(不含路径分隔符或 ..)"
    for rel, abs_path, _cat in _doc_sources():
        if rel == n:
            for _c, root, _p in DOC_SOURCES:
                try:
                    Path(abs_path).relative_to(Path(root).resolve())
                    return abs_path, None
                except ValueError:
                    continue
            return None, "路径不在白名单内"
    # 找不到时不要只说「白名单外」—— 模型会误读成「没权限」而放弃。
    # 明确否认权限问题,给出最接近的候选,并指向「不传 doc 搜全部」。
    key = n.lower().replace("_", "").replace("-", "").removesuffix(".md")
    cands = []
    for rel, _a, _c in _doc_sources():
        rkey = rel.lower().replace("_", "").replace("-", "").removesuffix(".md")
        if key and (key in rkey or rkey in key):
            cands.append(rel)
    msg = f"没有名为「{n}」的文档 —— 这不是权限问题,是文件名不对。"
    if cands:
        msg += " 最接近的:" + "、".join(cands[:5]) + "。"
    return None, msg + " 建议**不要指定 doc**,留空即可一次搜全部可读文档;完整清单用 list_docs 查。"


@server.tool()
def list_docs(category: str = "") -> str:
    """列出可读的**背诵手册 / 考情分析**文档(只读)。

    这些是已经检索并总结好的产物,比重新检索课本快且省。回答专业课问题时,
    可以先看这里有没有对应章节的整理稿。

    Args:
        category: 可选过滤,填「背诵手册」或「考情分析」(留空=全部)。

    Returns:
        {"ok":true,"count":N,"groups":{"背诵手册":[...],"考情分析":[...]}}
        用返回的文件名调用 grep_docs / read_doc。
    """
    srcs = _doc_sources()
    cat = (category or "").strip()
    groups: dict[str, list] = {}
    for rel, _abs, c in srcs:
        if cat and c != cat:
            continue
        groups.setdefault(c, []).append(rel)
    total = sum(len(v) for v in groups.values())
    return _j({
        "ok": True, "count": total, "groups": groups,
        "note": "只读;用 grep_docs 搜内容、read_doc 读区间。测试卷/答案等未开放。",
    })


@server.tool()
def grep_docs(pattern: str, doc: str = "", max_hits: int = 30, regex: bool = False) -> str:
    """在背诵手册 / 考情分析里按内容搜行(类似 grep,只读)。

    Args:
        pattern:  搜索词(默认按**纯文本**匹配;多个词用 | 分隔表示「任一命中」)。
        doc:      限定单个文件名;**强烈建议留空**(留空=一次搜全部 46 个文档)。
                  留空是常规做法 —— 不要凭记忆猜文件名,猜错会白白浪费一次调用。
        max_hits: 最多返回行数,1~60,默认 30。
        regex:    为 true 时把 pattern 当正则(默认 false,纯文本更安全)。

    Returns:
        {"ok":true,"count":N,"hits":[{"doc","line","text"}]} —— 命中行按文档+行号排序。
        再用 read_doc(doc, start=line-2, lines=20) 读上下文。
    """
    if not isinstance(pattern, str) or not pattern.strip():
        return _j({"ok": False, "error": "pattern 不能为空"})
    pat = pattern.strip()
    if len(pat) > DOC_MAX_PATTERN_CHARS:
        return _j({"ok": False, "error": f"pattern 过长(上限 {DOC_MAX_PATTERN_CHARS} 字符)"})
    try:
        cap = max(1, min(int(max_hits), DOC_MAX_HITS))
    except Exception:  # noqa: BLE001
        cap = 30

    if regex:
        try:
            rx = re.compile(pat)
        except re.error as exc:
            return _j({"ok": False, "error": f"正则无效:{exc}"})
        def hit(line: str) -> bool:
            return rx.search(line) is not None
    else:
        terms = [t for t in pat.split("|") if t.strip()]
        def hit(line: str) -> bool:
            return any(t in line for t in terms)

    srcs = _doc_sources()
    if doc:
        abs_path, err = _doc_resolve(doc)
        if err:
            return _j({"ok": False, "error": err})
        srcs = [s for s in srcs if s[0] == doc.strip()]

    hits = []
    if doc:
        # 单文档查询:不限额,扫满 cap 为止
        for rel, abs_path, _cat in srcs:
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as f:
                    for ln, line in enumerate(f, start=1):
                        if hit(line):
                            hits.append({"doc": rel, "line": ln,
                                         "text": line.strip()[:DOC_MAX_LINE_CHARS]})
                            if len(hits) >= cap:
                                break
            except OSError:
                continue
            if len(hits) >= cap:
                break
    else:
        # 全局搜索:必须先按文档收齐,再分配额度。
        # 实测教训:直接顺序扫 + 扫满即停,会把 40 行配额全给前两个文档
        # (搜「维生素」时 40 行被 612_amino_acid_... 和 612_HMG_topic 吃光,
        #  真正该看的 612_vitamin_coenzyme_recite.md 与考情分析一行都进不来)。
        per_doc_all = []
        for rel, abs_path, _cat in srcs:
            cur = []
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as f:
                    for ln, line in enumerate(f, start=1):
                        if hit(line):
                            cur.append({"doc": rel, "line": ln,
                                        "text": line.strip()[:DOC_MAX_LINE_CHARS]})
            except OSError:
                continue
            if cur:
                per_doc_all.append((rel, cur))

        # 第一轮:每个命中文档先各占 1 行(确保所有相关文档都露面,
        #        特别是考情分析 —— 它常排在手册后面,配额制下很容易一行都进不来)
        for rel, cur in per_doc_all:
            if len(hits) >= cap:
                break
            hits.append(cur[0])
        # 第二轮:轮转补齐(round-robin),保证各文档被均匀加深而不是被前几个吃光
        idx = 1
        while len(hits) < cap:
            added = False
            for rel, cur in per_doc_all:
                if len(hits) >= cap:
                    break
                if idx < len(cur):
                    hits.append(cur[idx])
                    added = True
            if not added:
                break
            idx += 1
        hits.sort(key=lambda h: (h["doc"], h["line"]))

    return _j({"ok": True, "pattern": pat, "count": len(hits),
               "docs_hit": len({h["doc"] for h in hits}),
               "truncated": len(hits) >= cap, "hits": hits})


@server.tool()
def read_doc(doc: str, start: int = 1, lines: int = DOC_DEFAULT_READ_LINES) -> str:
    """读某个背诵手册 / 考情分析文档的指定行区间(只读)。

    Args:
        doc:   文件名(见 list_docs),如 "612_glycometabolism_recite.md"。
        start: 起始行号(1-based),默认 1。
        lines: 读取行数,1~400,默认 120。

    Returns:
        {"ok":true,"doc","start","end","total_lines","text"}
        先用 grep_docs 定位行号,再来这里读上下文,避免整篇读入。
    """
    abs_path, err = _doc_resolve(doc)
    if err:
        return _j({"ok": False, "error": err})
    try:
        s = max(1, int(start))
    except Exception:  # noqa: BLE001
        s = 1
    try:
        n = max(1, min(int(lines), DOC_MAX_READ_LINES))
    except Exception:  # noqa: BLE001
        n = DOC_DEFAULT_READ_LINES

    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as exc:
        return _j({"ok": False, "error": f"读取失败:{exc}"})

    total = len(all_lines)
    s = min(s, total) if total else 1
    seg = all_lines[s - 1:s - 1 + n]
    return _j({
        "ok": True, "doc": doc.strip(), "start": s, "end": s - 1 + len(seg),
        "total_lines": total,
        "text": "".join(x[:DOC_MAX_LINE_CHARS * 3] for x in seg),
    })


@server.tool()
def list_sources() -> str:
    """列出可检索的库与能力边界。"""
    return _j({
        "ok": True,
        "textbook_and_guideline_collections": ALLOWED_COLLECTIONS,
        "exam_collections": "历年真题库 612-Y1996..Y2026 / 888-Y2005..Y2026(每年一个 collection,**按年检索,不逐个列出**)",
        "tools": [
            "search_textbooks(query, top_k, collection) — 课本 7 库 + 考纲 2 库;每条结果含 location(真实层级链)",
            "search_exam_papers(query, top_k) — 历年真题库(零散记录,非完整原卷)",
            "get_toc(collection, keyword) — 某本书的真实目录树(章 → 节 → 小节)",
            "get_chunk(chunk_id) — 取任一块的完整原文",
            "get_chunk_images(chunk_id, max_images) — 取块内插图的可发送路径(写 MEDIA:<path> 即发出)",
            "get_doc_images(doc, start, lines, max_images) — 取手册/考情某段的插图路径",
            "list_docs(category) — 列出可读的背诵手册 / 考情分析",
            "grep_docs(pattern, doc, max_hits, regex) — 在手册/考情里搜行(类 grep)",
            "read_doc(doc, start, lines) — 读手册/考情的指定行区间",
        ],
        "doc_access": {
            "背诵手册": "/srv/bio-knowledge/recite/*.md(25 个)",
            "考情分析": "/srv/bio-knowledge/*_analysis.md(21 个)",
            "note": "只读白名单;拒路径穿越;测试卷/答案/notes 未开放。模型侧无 file 工具,只能经此 MCP 访问。",
        },
        "note": "访问范围:课本/考纲/真题经检索类工具;背诵手册/考情分析经 list_docs/grep_docs/read_doc(只读白名单)。"
                "不可访问测试卷答案、notes 草稿、其他资料或任何系统文件。",
        "limits": {"max_query_chars": MAX_QUERY_CHARS, "max_top_k": MAX_TOP_K},
    })


# ---------------------------------------------------------------- 插图发送
# 把「课本块 / 背诵手册」里引用的插图映射成**导出目录内**的绝对路径,供模型写
# `MEDIA:<path>` 发送。导出目录用硬链接重建(不是符号链接 —— validate_media_delivery_path
# 的 resolve() 会穿透 symlink,那样路径会落到 docs/ 下使白名单失效)。
# ⚠️ 安全:只回 image_export 内的路径;只读;限额;拒绝任何白名单外的路径。
IMAGE_EXPORT_ROOT = "/srv/bio-knowledge/image_export"
IMAGE_MAX_PER_CALL = 6
_img_index = None
_RE_IMG_NODE = re.compile(r"!\[[^\]]*\]\((images/[^)\s]+)\)")            # 课本块里的图引用
_RE_IMG_ANY = re.compile(r"!\[[^\]]*\]\((?!https?://)([^)\s]+\.(?:jpg|jpeg|png|webp))", re.I)


def _image_index() -> dict:
    """文件名 → 导出目录内绝对路径(懒建一次,缓存在进程内)。"""
    global _img_index
    if _img_index is None:
        idx = {}
        root = Path(IMAGE_EXPORT_ROOT)
        if root.is_dir():
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    idx.setdefault(p.name, str(p))
        _img_index = idx
    return _img_index


def _export_lookup(ref: str):
    """把图引用映射成导出目录内路径;不在导出目录则 None(绝不回白名单外路径)。"""
    if not ref:
        return None
    r = ref.strip().lstrip("./")
    root = Path(IMAGE_EXPORT_ROOT)
    # ① 课本块:images/<sha256>.jpg → 按文件名在索引里找
    hit = _image_index().get(os.path.basename(r))
    if hit:
        return hit
    # ② 手册:assets/<topic>/<name>.jpg → recite/<topic>/<name>.jpg
    if r.startswith("assets/"):
        cand = root / "recite" / r[len("assets/"):]
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            return None
    # ③ 兜底:recite/ 或 textbook/ 开头的相对引用
    for pre in ("recite/", "textbook/"):
        if r.startswith(pre):
            cand = root / r
            try:
                if cand.is_file():
                    return str(cand)
            except OSError:
                return None
    return None


def _collect_images(text: str, cap: int):
    images, seen = [], set()
    for ref in _RE_IMG_NODE.findall(text) + _RE_IMG_ANY.findall(text):
        if len(images) >= cap:
            break
        if ref in seen:
            continue
        seen.add(ref)
        p = _export_lookup(ref)
        if p:
            images.append({"ref": ref, "path": p})
    return images


@server.tool()
def get_chunk_images(chunk_id: str, max_images: int = 3) -> str:
    """取某个课本块的插图,返回可直接发送的图片路径。

    **用法**:拿到 path 后,在你的回复里**单独一行写 `MEDIA:<path>`** ——
    系统会把那张图作为图片消息发给对方,`MEDIA:` 那一行不会显示成文字。

    Args:
        chunk_id:   由 search_textbooks / search_exam_papers 返回的块 id。
        max_images: 最多返回几张,1~6,默认 3。

    Returns:
        {"ok":true,"count":N,"images":[{"ref":"images/xxx.jpg","path":"/abs/…jpg"}],
         "note":"…"}。count=0 表示这个块没有可发送的插图。
    """
    if not isinstance(chunk_id, str) or not chunk_id.strip():
        return _j({"ok": False, "error": "chunk_id 不能为空"})
    cid = chunk_id.strip()
    base = os.path.basename(cid)
    looks_exam = any(base.startswith(p) for p in ("612-Y", "888-Y"))
    chroma_path = EXAM_CHROMA_PATH if looks_exam else TEXTBOOK_CHROMA_PATH

    raw = ""
    rc, out, _err = _run(KB_GET, [cid], extra_env={"BIO_CHROMA_PATH": chroma_path})
    if rc == 0 and '"error"' not in out:
        raw = out
    else:
        alt = TEXTBOOK_CHROMA_PATH if looks_exam else EXAM_CHROMA_PATH
        rc2, out2, _e2 = _run(KB_GET, [cid], extra_env={"BIO_CHROMA_PATH": alt})
        if rc2 == 0 and '"error"' not in out2:
            raw = out2
    if not raw:
        return _j({"ok": False, "error": "取块失败,读不到插图引用"})

    try:
        text = (json.loads(raw) or {}).get("text") or raw
    except Exception:  # noqa: BLE001
        text = raw

    try:
        cap = max(1, min(int(max_images), IMAGE_MAX_PER_CALL))
    except Exception:  # noqa: BLE001
        cap = 3

    images = _collect_images(text, cap)
    return _j({
        "ok": True, "count": len(images), "images": images,
        "note": ("把 path 以 `MEDIA:<path>` 单独一行写进回复即可发送该图"
                 if images else "这个块没有可发送的插图"),
    })


@server.tool()
def get_doc_images(doc: str, start: int = 1, lines: int = 0, max_images: int = 3) -> str:
    """取背诵手册 / 考情分析指定行区间里的插图,返回可直接发送的图片路径。

    先 grep_docs 定位到含图的段落,再来这里拿路径;或直接给较大区间。

    Args:
        doc:        文件名(见 list_docs),如 "612_genetic_engineering_recite.md"。
        start:      起始行号(1-based),默认 1。
        lines:      读取行数(0 = 读到文件尾,最多 400)。
        max_images: 最多返回几张,1~6,默认 3。

    Returns:
        {"ok":true,"count":N,"images":[...],"note":"…"}
    """
    abs_path, err = _doc_resolve(doc)
    if err:
        return _j({"ok": False, "error": err})
    try:
        st = max(1, int(start))
    except Exception:  # noqa: BLE001
        st = 1
    try:
        ln = max(0, min(int(lines), DOC_MAX_READ_LINES))
    except Exception:  # noqa: BLE001
        ln = 0
    try:
        cap = max(1, min(int(max_images), IMAGE_MAX_PER_CALL))
    except Exception:  # noqa: BLE001
        cap = 3

    chunk = []
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, start=1):
                if i < st:
                    continue
                if ln and i >= st + ln:
                    break
                chunk.append(line)
    except OSError as exc:
        return _j({"ok": False, "error": f"读取失败:{exc}"})

    images = _collect_images("".join(chunk), cap)
    return _j({
        "ok": True, "doc": os.path.basename(abs_path),
        "start": st, "end": st + len(chunk) - 1,
        "count": len(images), "images": images,
        "note": ("把 path 以 `MEDIA:<path>` 单独一行写进回复即可发送该图"
                 if images else "这段没有可发送的插图"),
    })


if __name__ == "__main__":
    # 启动前自检:key 与脚本存在性(缺失时写 stderr,便于排障,但不阻塞启动)
    if not os.environ.get("DASHSCOPE_API_KEY"):
        print("[rag] WARN: DASHSCOPE_API_KEY 未设置,检索调用会失败", file=sys.stderr)
    for p in (PY, KB_SEARCH, KB_GET):
        if not os.path.exists(p):
            print(f"[rag] WARN: 缺少 {p}", file=sys.stderr)
    for p in (TEXTBOOK_CHROMA_PATH, EXAM_CHROMA_PATH):
        if not os.path.isdir(p):
            print(f"[rag] WARN: 缺少库目录 {p}", file=sys.stderr)
    server.run(transport="stdio")
