#!/usr/bin/env python3
"""ChromaDB 全库健康诊断脚本。

遍历 chroma_db 与 chroma_exam_db 的所有 collection,检查:
  1. 文档数(col.count())
  2. hnsw 向量索引文件状态(data_level0.bin / link_lists.bin / length.bin / index_metadata.pickle)
  3. 元数据一致性(pickle 记录的 total_elements_added vs data_level0.bin 实际容量)
  4. 实际检索验证(复用 1 次 embedding,对每个 collection 跑本地 query)

输出:JSON(机器可读)+ 人类可读摘要。状态判定:
  OK      — 文件齐全、容量匹配、检索正常
  WARN    — 容量略低于记录(部分向量缺失)但检索仍可用
  BROKEN  — link_lists.bin=0 或 容量严重不足 或 检索返回 0/报错

用法:
  cd /srv/bio-kb
  set -a && source ~/.hermes/.env && set +a
  ./.venv/bin/python diagnose_db.py [--json] [--query "测试检索词"]
"""
import argparse
import datetime
import json
import math
import os
import pickle
import sqlite3
import sys

DB_PATHS = [
    "/srv/bio-kb/chroma_db",
    "/srv/bio-kb/chroma_exam_db",
]
# 若脚本在 chroma 目录内运行,允许相对路径
_HERE = os.path.dirname(os.path.abspath(__file__))
if not os.path.exists(DB_PATHS[0]):
    DB_PATHS = [os.path.join(_HERE, "chroma_db"), os.path.join(_HERE, "chroma_exam_db")]

STATE_OK, STATE_WARN, STATE_BROKEN = "OK", "WARN", "BROKEN"


def fmt_dt(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def read_hnsw_files(seg_dir: str):
    """读取 hnsw 段目录的文件大小与 mtime。"""
    files = {}
    if not os.path.isdir(seg_dir):
        return None
    for fname in ("header.bin", "data_level0.bin", "link_lists.bin", "length.bin", "index_metadata.pickle"):
        fp = os.path.join(seg_dir, fname)
        if os.path.exists(fp):
            st = os.stat(fp)
            files[fname] = {"size": st.st_size, "mtime": fmt_dt(st.st_mtime)}
    return files


def read_pickle_total(seg_dir: str):
    """读 index_metadata.pickle 里的 total_elements_added 与 id_to_label 大小。"""
    try:
        with open(os.path.join(seg_dir, "index_metadata.pickle"), "rb") as f:
            meta = pickle.load(f)
        return {
            "total_elements_added": meta.get("total_elements_added"),
            "id_to_label": len(meta.get("id_to_label", {})),
        }
    except Exception:
        return None


def data_capacity(data_size: int, dim: int) -> int:
    """data_level0.bin 容量 = 字节数 / (维度×4)。"""
    if not data_size or not dim:
        return 0
    return data_size // (dim * 4)


def inspect_collection(client, db_path: str, name: str, query_vec, query_k: int = 1):
    """诊断单个 collection。"""
    rec = {"collection": name, "db": os.path.basename(db_path), "state": STATE_OK, "issues": []}
    try:
        col = client.get_collection(name)
    except Exception as e:
        rec.update({"state": STATE_BROKEN, "issues": [f"get_collection 失败: {str(e)[:120]}"]})
        return rec

    rec["count"] = col.count()
    try:
        rec["metadata"] = col.metadata or {}
    except Exception:
        rec["metadata"] = {}

    # dimension 从 collection 拿
    dim = None
    try:
        dim = col._model.get("dimension") if hasattr(col, "_model") else None
    except Exception:
        dim = None
    if not dim:
        try:
            dim = rec["metadata"].get("dimension")
        except Exception:
            dim = None
    rec["dimension"] = dim

    # 定位 VECTOR segment 目录(通过 sqlite segments 表)
    seg_dir, seg_id = None, None
    try:
        conn = sqlite3.connect(os.path.join(db_path, "chroma.sqlite3"))
        cur = conn.cursor()
        cid = cur.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()
        if cid:
            row = cur.execute("SELECT id FROM segments WHERE collection=? AND scope='VECTOR'", (cid[0],)).fetchone()
            if row:
                seg_id = row[0]
                seg_dir = os.path.join(db_path, seg_id)
        conn.close()
    except Exception as e:
        rec["issues"].append(f"sqlite 段定位失败: {str(e)[:80]}")

    rec["segment_id"] = seg_id
    rec["hnsw_files"] = read_hnsw_files(seg_dir) if seg_dir else None
    pkl = read_pickle_total(seg_dir) if seg_dir else None
    rec["total_elements_added"] = (pkl or {}).get("total_elements_added")
    rec["id_to_label"] = (pkl or {}).get("id_to_label")

    # 容量对比
    hf = rec["hnsw_files"] or {}
    data_size = (hf.get("data_level0.bin") or {}).get("size", 0)
    link_size = (hf.get("link_lists.bin") or {}).get("size", 0)
    cap = data_capacity(data_size, dim or 1024)
    rec["data_capacity"] = cap
    expected = rec["total_elements_added"] or rec["count"]

    # 实际检索验证(若提供 query_vec)——最可靠的损坏判据
    if query_vec is not None and rec["count"] > 0:
        try:
            res = col.query(query_embeddings=[query_vec], n_results=query_k, include=["documents"])
            rec["query_test"] = len((res.get("documents") or [[]])[0] or [])
        except Exception as e:
            rec["query_test"] = -1
            rec["query_error"] = str(e)[:120]
    else:
        rec["query_test"] = None

    # 状态判定
    if rec["count"] == 0:
        rec["state"] = STATE_WARN
        rec["issues"].append("空 collection(0 篇文档)")

    # 检索验证优先:返回 0 或报错 = 真损坏(覆盖其他规则)
    qt = rec.get("query_test")
    if qt == 0:
        rec["state"] = STATE_BROKEN
        rec["issues"].append("实际检索返回 0 条(索引不可用)")
    elif qt == -1:
        rec["state"] = STATE_BROKEN
        rec["issues"].append("实际检索报错")

    # 容量对比(小库 link_lists.bin=0 属正常,不作损坏判据)
    if rec["count"] > 0 and cap < rec["count"] * 0.9:
        rec["state"] = STATE_BROKEN
        rec["issues"].append(f"data_level0.bin 容量 {cap} << 文档数 {rec['count']}(向量缺失 ~{100 - 100*cap/max(rec['count'],1):.0f}%)")
    elif cap < rec["count"]:
        rec["state"] = STATE_WARN
        rec["issues"].append(f"data_level0.bin 容量 {cap} < 文档数 {rec['count']}(部分向量缺失)")

    if link_size == 0 and rec["count"] > 0 and rec["count"] > 200:
        rec["issues"].append("link_lists.bin=0 字节(大库图连接丢失)")

    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--query", default="基因 表达 调控 复制 转录", help="检索验证用测试词")
    args = ap.parse_args()

    import chromadb
    try:
        from llama_index.embeddings.dashscope import DashScopeEmbedding
        from llama_index.core import Settings
    except Exception:
        DashScopeEmbedding = None

    results = []
    query_vec = None
    if DashScopeEmbedding is not None and os.environ.get("DASHSCOPE_API_KEY"):
        try:
            emb = DashScopeEmbedding(model_name="qwen3.7-text-embedding",
                                     api_key=os.environ["DASHSCOPE_API_KEY"])
            query_vec = emb.get_query_embedding(args.query)
        except Exception as e:
            print(f"# 生成测试向量失败(跳过检索验证): {str(e)[:120]}", file=sys.stderr)

    for db_path in DB_PATHS:
        if not os.path.isdir(db_path):
            print(f"# 路径不存在,跳过: {db_path}", file=sys.stderr)
            continue
        client = chromadb.PersistentClient(path=db_path)
        names = [c.name for c in client.list_collections()]
        for name in sorted(names):
            rec = inspect_collection(client, db_path, name, query_vec)
            results.append(rec)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return

    # 人类可读摘要
    by_state = {STATE_OK: 0, STATE_WARN: 0, STATE_BROKEN: 0}
    print(f"{'状态':7s} {'库':12s} {'collection':42s} {'count':>5s} {'容量':>5s} {'记录':>5s} {'link':>6s} {'检索':>4s}  备注")
    print("-" * 120)
    for r in results:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        hf = r["hnsw_files"] or {}
        link = (hf.get("link_lists.bin") or {}).get("size", "-")
        qt = r.get("query_test", "-")
        cap = r.get("data_capacity", "-")
        exp = r.get("total_elements_added", "-")
        idl = r.get("id_to_label", "-")
        issue = "; ".join(r["issues"][:2]) if r["issues"] else ""
        print(f"{r['state']:7s} {r['db']:12s} {r['collection']:42s} {r['count']:5d} {cap!s:>5s} {exp!s:>5s} {str(idl):>5s} {str(link):>6s} {str(qt):>4s}  {issue}")
    print("-" * 120)
    print(f"汇总: OK={by_state[STATE_OK]}  WARN={by_state[STATE_WARN]}  BROKEN={by_state[STATE_BROKEN]}  总计={len(results)}")


if __name__ == "__main__":
    main()
