# 03 检索

| 文件 | 作用 |
|---|---|
| `retriever.py` | 检索器，支持单库与跨库，输出格式化文本或结构化列表 |
| `kb_search.py` | 命令行入口，输出结构化 JSON，包含稳定的 `chunk_id` |
| `kb_get_chunk.py` | 按 `chunk_id` 取回块的完整原文 |

## 检索路径

`search_fast` 是默认路径：执行一次远程 embedding，然后使用 ChromaDB 原生的 `query_embeddings` 在本地逐库检索，最后执行一次远程 rerank。跨 50 个 collection 的检索从约 40 秒降至 2 至 5 秒，结果与原有实现一致。

等价性依赖两个前提，写在代码注释中：`distance` 转 `score` 必须使用 `exp(-distance)`；构造 rerank 输入必须经由 `metadata_dict_to_node`，手工拼接 `TextNode` 会将 `document_id`、`_node_type` 带入 rerank 文本，导致分数变化。

## 需要修改的位置

- `EMBED_MODEL`、`RERANK_MODEL`、`DASHSCOPE_API_KEY`。
- `CHROMA_PATH`（教材库）与真题库路径。
- collection 名单：`kb_get_chunk.py` 只扫描白名单内的 collection，并以 collection 名的前 16 个字符匹配 `chunk_id` 前缀。collection 改名后，旧 `chunk_id` 无法定位。

## 参数行为

`--top-k` 与 `--rerank` 是独立参数，需要 5 条结果时必须同时传入 5；只传 `--top-k` 时，结果数受 `--rerank` 默认值 5 限制。

两个脚本的库路径接口不同：`kb_search.py` 使用 `--chroma` 参数，`kb_get_chunk.py` 仅读取环境变量 `BIO_CHROMA_PATH`。封装时需要分别传入。
