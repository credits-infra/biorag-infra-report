#!/usr/bin/env python3
"""决定性验证:清空 embeddings_queue 后 query() 是否仍正常(#7463 判据)。

背景:chroma 低于 sync_threshold 时索引不落盘,检索靠每次打开回放 WAL(queue)。
若索引未持久化,清空 queue 后 get() 命中但 query() 返回空(磁盘索引为空)。
本脚本在副本上执行:备份 queue → 清空 → 新进程重开 → count/get/query 验证。

用法:
  cd /srv/bio-kb && set -a && source ~/.hermes/.env && set +a
  ./.venv/bin/python scripts/test_persist_proof.py /tmp/chroma_db_test <collection>
"""
import argparse
import sqlite3
import shutil
from pathlib import Path

import chromadb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db_path")
    ap.add_argument("collection")
    args = ap.parse_args()

    db_path = Path(args.db_path)
    sqlite_file = db_path / "chroma.sqlite3"

    conn = sqlite3.connect(sqlite_file)
    cur = conn.cursor()
    backup = cur.execute("SELECT * FROM embeddings_queue").fetchall()
    print(f"embeddings_queue 备份 {len(backup)} 行 → /tmp/queue_backup_{args.collection}.sqlite")
    shutil.copy(sqlite_file, f"/tmp/queue_backup_{args.collection}.sqlite")
    cur.execute("DELETE FROM embeddings_queue")
    conn.commit()
    conn.close()
    print("已清空 embeddings_queue")

    client = chromadb.PersistentClient(path=str(db_path))
    col = client.get_collection(args.collection)
    print(f"\n重开后 count={col.count()}")

    try:
        data = col.get(limit=2)
        print(f"get() 返回 {len(data['ids'])} 条")
    except Exception as e:
        print(f"get() 报错: {str(e)[:120]}")

    try:
        data = col.get(limit=2, include=["embeddings"])
        embs = data["embeddings"]
        if embs:
            vec = embs[0].tolist() if hasattr(embs[0], "tolist") else embs[0]
            res = col.query(query_embeddings=[vec], n_results=2, include=["documents"])
            docs = res["documents"][0]
            got = len(docs) if docs is not None else 0
            print(f"query() 返回 {got} 条 {'✅ 索引已持久化' if got else '❌ 索引为空,靠 WAL 才能检索'}")
    except Exception as e:
        print(f"query() 报错: {str(e)[:150]}")


if __name__ == "__main__":
    main()
