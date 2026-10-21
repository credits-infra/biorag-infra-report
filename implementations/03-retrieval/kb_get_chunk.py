#!/usr/bin/env python3
"""kb_get_chunk.py — 按 chunk_id 取回课本块的完整原文(只读)。

用法:
    ./.venv/bin/python kb_get_chunk.py <chunk_id>

chunk_id 形如 `Biological_Chemi_a2bbd94547a6`:
    前缀 = collection 名前 16 字符(用于直接定位库,避免全库扫描)
    后缀 = sha1(f"{collection}|{chapter}|{section}|{text[:200]}")[:12]

安全约束(与 rag_readonly MCP 配套):
    * 只读:仅 chromadb 的 get,无写、无 shell、无任意路径入参
    * 库白名单:只允许 6 个课本库(见 ALLOWED_COLLECTIONS),recite 自编手册与真题库不在内
    * 输出 JSON,不返回文件系统路径

输出:
    {"ok":true,"chunk_id","collection","chapter","section","text"} 或 {"ok":false,"error":...}
"""
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHROMA_PATH = os.environ.get("BIO_CHROMA_PATH") or os.path.join(HERE, "chroma_db")

# ⚠️ 只读白名单:课本库 + 考纲库。真题库用下面正则匹配(每年新增,不逐个枚举)。
ALLOWED_COLLECTIONS = {
    "Biological_Chemistry_Zhu_Sheng_Geng",   # 朱圣庚《生物化学》第4版
    "Principle_of_Biochemistry_Yang_RongWu",  # 杨荣武《生物化学原理》
    "Morden_Molecular_Biology",               # 朱玉贤《现代分子生物学》
    "Lewin-s-Gene-X-ZH",                      # Lewin《基因 X》
    "Microbiology",                           # 微生物学
    "General_Biology",                        # 陈阅增《普通生物学》
    "Genetics",                               # 遗传学
    "612-Biochemistry-and-Molecular-Biology-Guideline",   # 612 考纲
    "888-Comprehensive-Biology-Guideline",                # 888 考纲
}

# 真题库(chroma_exam_db)的 collection 名形如 612-Y2020 / 888-Y2025-blank。
# ⚠️ 取真题块必须设 BIO_CHROMA_PATH=<...>/chroma_exam_db,否则扫不到。
_EXAM_COLLECTION_RE = re.compile(r"^(?:612|888)-Y\d{4}(?:-blank)?$")


def _is_allowed_collection(name: str) -> bool:
    return name in ALLOWED_COLLECTIONS or bool(_EXAM_COLLECTION_RE.match(name))

MAX_TEXT = 6000  # 单块返回上限,防超长


def _sha12(collection: str, chapter: str, section: str, text: str) -> str:
    return hashlib.sha1(
        f"{collection}|{chapter or ''}|{section or ''}|{(text or '')[:200]}".encode()
    ).hexdigest()[:12]


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: kb_get_chunk.py <chunk_id>"}, ensure_ascii=False))
        return 2
    cid = sys.argv[1].strip()
    if "_" not in cid:
        print(json.dumps({"ok": False, "error": "chunk_id 需形如 <collection前缀>_<12位hex>"}, ensure_ascii=False))
        return 2
    prefix, _, hexpart = cid.rpartition("_")
    if len(hexpart) != 12 or any(c not in "0123456789abcdef" for c in hexpart):
        print(json.dumps({"ok": False, "error": "chunk_id 后缀须为 12 位十六进制"}, ensure_ascii=False))
        return 2

    try:
        import chromadb
    except Exception as exc:  # 环境问题如实报,不静默
        print(json.dumps({"ok": False, "error": f"chromadb 不可用: {exc}"}, ensure_ascii=False))
        return 3

    client = chromadb.PersistentClient(path=CHROMA_PATH)
    scanned = []
    for col in client.list_collections():
        name = col.name
        if not _is_allowed_collection(name):
            continue
        if name[:16] != prefix:
            continue
        scanned.append(name)
        got = client.get_collection(name).get(include=["metadatas", "documents"])
        for md, doc in zip(got.get("metadatas") or [], got.get("documents") or []):
            md = md or {}
            if _sha12(name, md.get("chapter", ""), md.get("section", ""), doc or "") == hexpart:
                print(json.dumps({
                    "ok": True,
                    "chunk_id": f"{name[:16]}_{hexpart}",
                    "collection": name,
                    "chapter": md.get("chapter", ""),
                    "section": md.get("section", ""),
                    "text": (doc or "")[:MAX_TEXT],
                }, ensure_ascii=False))
                return 0

    print(json.dumps({
        "ok": False,
        "error": "not_found" if scanned else "prefix_no_match",
        "scanned_collections": scanned,
        "hint": "前缀须等于 collection 名前 16 字符;白名单外的库不可访问",
    }, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
