# 02 向量库诊断与修复

四个脚本，对应 ChromaDB 1.5.9 的持久化缺陷（上游 issue #6975、#7090、#7463；修复 PR #7047 未合并）。

| 脚本 | 作用 | 使用时机 |
|---|---|---|
| `diagnose_db.py` | 全库诊断：count、文件容量、pickle 记录数、实际检索验证 | 任何阶段，优先执行 |
| `test_flush.py` | 通过幂等 re-upsert 使写计数超过 `sync_threshold`，触发一次实际落盘 | 怀疑尾部记录未落盘时 |
| `test_persist_proof.py` | 落盘验证：清空 queue 后重启进程，检索仍正常 | 需要确认索引已实际落盘时 |
| `restore_from_queue.py` | 从 `embeddings_queue` 恢复 `get()` 报错的 collection | 检索结果持续为空时 |

## 操作约束

- `test_persist_proof.py` 会执行 `DELETE FROM embeddings_queue` 清空整张表，只能对副本执行；每个破坏性测试使用一份新副本，否则会污染同一副本中其他未落盘 collection 的 WAL。
- 修改生产库之前，先用 `cp -a` 复制到 `/tmp` 演练，确认无误后再对正式库执行。
- 「检索正常」不等于「索引完整」：queue 会被回放，向量可以查到但从未落盘。判据是 `data_level0.bin` 的体积与 `count()` 是否匹配。
- 不要调小 `sync_threshold`。阈值过小会使每次写入触发整段重写；实测 26,000 条记录产生约 13,000 次重写，并导致 compactor 停止响应。

## 依赖

仅依赖 `chromadb`。`diagnose_db.py` 的数据库路径在文件顶部的 `DB_PATHS` 中配置，其余脚本通过位置参数接收数据库路径，运行前需要传入实际路径。
