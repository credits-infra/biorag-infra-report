#!/usr/bin/env python3
"""副本演练:幂等 re-upsert 触发 HNSW 落盘(#7463 官方认可的 workaround)。

用法(先在副本验证,再对正式库执行):
  cp -a /path/chroma_db /tmp/chroma_db_test
  cd /srv/bio-kb && set -a && source ~/.hermes/.env && set +a
  ./.venv/bin/python scripts/test_flush.py /tmp/chroma_db_test <collection>

原理:读回已有记录(ids+embeddings+metadatas+documents)原样 upsert,
推进写计数跨过 sync_threshold(默认 1000)触发 _persist() 把 WAL 尾数落盘。
无写放大,一次只触发一次;不调小 sync_threshold(见 MemPalace #2106)。

判据:data_level0.bin 容量与 pickle_total 应达到 count;queue 由 chroma 分批清理,
不立即归零属正常。落盘后跑 scripts/test_persist_proof.py 做清空-queue 决定性验证。
"""
import argparse
import pickle
import sqlite3
import time
from pathlib import Path

import chromadb


def queue_count(db_path: str, collection_name: str) -> int:
    conn = sqlite3.connect(Path(db_path) / "chroma.sqlite3")
    cur = conn.cursor()
    cid = cur.execute("SELECT id FROM collections WHERE name=?", (collection_name,)).fetchone()
    if not cid:
        conn.close()
        return -1
    n = cur.execute(
        "SELECT COUNT(*) FROM embeddings_queue WHERE topic LIKE ?", (f"%/{cid[0]}",)
    ).fetchone()[0]
    conn.close()
    return n


def hnsw_stats(db_path: str, collection_name: str):
    conn = sqlite3.connect(Path(db_path) / "chroma.sqlite3")
    cur = conn.cursor()
    cid = cur.execute("SELECT id FROM collections WHERE name=?", (collection_name,)).fetchone()
    row = cur.execute(
        "SELECT id FROM segments WHERE collection=? AND scope='VECTOR'", (cid[0],)
    ).fetchone()
    conn.close()
    if not row:
        return None
    seg_dir = Path(db_path) / row[0]
    stats = {}
    for fname in ("header.bin", "data_level0.bin", "link_lists.bin", "length.bin", "index_metadata.pickle"):
        fp = seg_dir / fname
        stats[fname] = fp.stat().st_size if fp.exists() else -1
    try:
        with open(seg_dir / "index_metadata.pickle", "rb") as f:
            meta = pickle.load(f)
        stats["pickle_total"] = meta.get("total_elements_added")
    except Exception as e:
        stats["pickle_total"] = f"ERR {str(e)[:50]}"
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db_path")
    ap.add_argument("collection")
    ap.add_argument("--reupsert-n", type=int, default=0,
                    help="re-upsert 多少条(0=全部;建议 >=1000 跨过阈值)")
    args = ap.parse_args()

    client = chromadb.PersistentClient(path=args.db_path)
    col = client.get_collection(args.collection)
    cnt = col.count()
    print(f"collection={args.collection} count={cnt}")
    print(f"before: queue={queue_count(args.db_path, args.collection)} hnsw={hnsw_stats(args.db_path, args.collection)}")

    t0 = time.time()
    batch_size = 500
    ids, embs, metas, docs = [], [], [], []
    for i in range(0, cnt, batch_size):
        data = col.get(limit=min(batch_size, cnt - i), offset=i,
                       include=["embeddings", "metadatas", "documents"])
        ids.extend(data["ids"]); embs.extend(data["embeddings"])
        metas.extend(data["metadatas"]); docs.extend(data["documents"])
    print(f"  读取 {len(ids)} 条 ({time.time()-t0:.1f}s)")

    n = args.reupsert_n or len(ids)
    t0 = time.time()
    for i in range(0, n, 200):
        sl = slice(i, i + 200)
        col.upsert(ids=ids[sl], embeddings=embs[sl], metadatas=metas[sl], documents=docs[sl])
        print(f"  upsert {min(i+200, n)}/{n} ({time.time()-t0:.1f}s)")
    print(f"after:  queue={queue_count(args.db_path, args.collection)} hnsw={hnsw_stats(args.db_path, args.collection)}")

    client2 = chromadb.PersistentClient(path=args.db_path)
    col2 = client2.get_collection(args.collection)
    print(f"\n新进程重开: count={col2.count()}")
    try:
        res = col2.query(query_embeddings=[embs[0].tolist() if hasattr(embs[0], "tolist") else embs[0]],
                         n_results=2, include=["documents"])
        got = len(res["documents"][0] or [])
        print(f"  query() 返回 {got} 条 {'✅' if got else '❌'}")
    except Exception as e:
        print(f"  query() 报错: {str(e)[:150]} ❌")


if __name__ == "__main__":
    main()
