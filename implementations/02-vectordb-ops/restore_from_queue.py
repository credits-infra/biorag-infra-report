#!/usr/bin/env python3
"""从 embeddings_queue 恢复彻底损坏的 chroma collection(不重新 embedding)。

适用:collection 检索返回 0、连 get() 都报 "Error getting embedding" 的损坏库。
数据其实没丢——全部在 sqlite 的 embeddings_queue 表(WAL):
  - vector 列  = 4096 字节 = 1024 维 float32(struct.unpack 直接还原)
  - metadata 列 = JSON 字符串,含 chroma:document(文档全文) + 用户元数据(book/part/chapter/section/exam/...)

流程:提取 queue → 删损坏 collection → 重建 → add(原 id+原向量+原文本,chunk_id 自动一致)
     → 补 re-upsert 累计写计数 >= 1000 触发 _persist() 落盘 → 新进程验证。

⚠️ 前提:queue 必须还在(勿在跑过 test_persist_proof.py 清空 queue 的副本上使用)。
⚠️ 先在 /tmp 副本上演练,确认后再对正式库执行。

用法:
  cd /srv/bio-kb
  set -a && source ~/.hermes/.env && set +a
  ./.venv/bin/python restore_from_queue.py <db_path> <collection>
"""
import argparse
import json
import pickle
import sqlite3
import struct
import sys
import time
from pathlib import Path

import chromadb

KEEP_META = ("book", "part", "chapter", "section", "exam", "year", "header", "type")


def load_from_queue(db_path: str, collection_name: str):
    """从 embeddings_queue 提取某 collection 的全部记录。"""
    conn = sqlite3.connect(Path(db_path) / "chroma.sqlite3")
    cur = conn.cursor()
    cid = cur.execute("SELECT id FROM collections WHERE name=?", (collection_name,)).fetchone()
    if not cid:
        conn.close()
        raise SystemExit(f"collection 不存在: {collection_name}")
    rows = cur.execute(
        "SELECT id, vector, metadata FROM embeddings_queue WHERE topic LIKE ? ORDER BY seq_id",
        (f"%/{cid[0]}",),
    ).fetchall()
    conn.close()

    records = []
    for eid, vec, md in rows:
        emb = list(struct.unpack(f"{len(vec)//4}f", vec)) if vec else None
        mdj = json.loads(md) if isinstance(md, str) else json.loads(md.decode())
        doc = mdj.get("chroma:document", "")
        meta = {k: v for k, v in mdj.items() if k in KEEP_META}
        records.append({"id": eid, "embedding": emb, "document": doc, "metadata": meta})
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db_path")
    ap.add_argument("collection")
    args = ap.parse_args()

    t0 = time.time()
    records = load_from_queue(args.db_path, args.collection)
    print(f"从 queue 提取 {len(records)} 条 ({time.time()-t0:.1f}s)")
    if not records:
        print("queue 为空,无需恢复(可能已持久化)")
        return
    sample = records[0]
    print(f"  样例: id={sample['id'][:12]}... 向量维={len(sample['embedding'])} "
          f"doc前40={sample['document'][:40]!r} meta={sample['metadata']}")

    client = chromadb.PersistentClient(path=args.db_path)

    print("\n重建 collection ...")
    try:
        client.delete_collection(args.collection)
        print("  已删除损坏 collection")
    except Exception as e:
        print(f"  delete 警告: {e}")
    col = client.create_collection(name=args.collection, metadata={"hnsw:space": "cosine"})
    print("  已重建")

    t0 = time.time()
    B = 100
    for i in range(0, len(records), B):
        chunk = records[i:i + B]
        col.add(
            ids=[r["id"] for r in chunk],
            embeddings=[r["embedding"] for r in chunk],
            documents=[r["document"] for r in chunk],
            metadatas=[r["metadata"] for r in chunk],
        )
        print(f"  add {min(i+B, len(records))}/{len(records)} ({time.time()-t0:.1f}s)")

    # 触发落盘:add 条数 < 1000 时补 re-upsert 累计写计数 >= 1000
    need = max(0, 1000 - len(records))
    if need > 0:
        print(f"\n触发落盘:补充 re-upsert {need} 次写操作(累计 >= 1000)...")
        t0 = time.time()
        for i in range(0, need, 100):
            chunk = records[i % len(records):(i % len(records)) + 100]
            col.upsert(
                ids=[r["id"] for r in chunk],
                embeddings=[r["embedding"] for r in chunk],
                documents=[r["document"] for r in chunk],
                metadatas=[r["metadata"] for r in chunk],
            )
        print(f"  re-upsert 完成 ({time.time()-t0:.1f}s)")
    else:
        print("\nadd 已跨阈值,无需补写")

    print(f"\n重建完成: count={col.count()}")

    # hnsw 文件验证
    conn = sqlite3.connect(Path(args.db_path) / "chroma.sqlite3")
    cur = conn.cursor()
    cid = cur.execute("SELECT id FROM collections WHERE name=?", (args.collection,)).fetchone()[0]
    row = cur.execute("SELECT id FROM segments WHERE collection=? AND scope='VECTOR'", (cid[0],)).fetchone()
    conn.close()
    if row:
        seg_dir = Path(args.db_path) / row[0]
        stats = {}
        for f in ("data_level0.bin", "link_lists.bin", "index_metadata.pickle"):
            fp = seg_dir / f
            stats[f] = fp.stat().st_size if fp.exists() else -1
        try:
            with open(seg_dir / "index_metadata.pickle", "rb") as f:
                stats["pickle_total"] = pickle.load(f).get("total_elements_added")
        except Exception as e:
            stats["pickle_total"] = f"ERR {e}"
        print("hnsw 文件:", stats)

    # 新进程重开验证
    print("\n--- 新进程重开验证 ---")
    client2 = chromadb.PersistentClient(path=args.db_path)
    col2 = client2.get_collection(args.collection)
    print("count =", col2.count())
    data = col2.get(limit=1, include=["embeddings"])
    emb0 = data["embeddings"][0]
    res = col2.query(query_embeddings=[emb0.tolist()], n_results=2, include=["documents"])
    got = len(res["documents"][0])
    print(f"query() 返回 {got} 条 {'✅' if got else '❌'}")


if __name__ == "__main__":
    main()
