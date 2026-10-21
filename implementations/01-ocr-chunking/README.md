# 01 入库

`ingest.py` 将 MinerU 的 Markdown 产物切分为块并写入 ChromaDB。

## 处理流程

1. 遍历 `docs/<书名>/*/full.md`。目录名由 MinerU 生成，形如 `生物化学_上册_5-9.pdf-<uuid>/`；页码范围由原文件名保留。
2. 第一段按标题边界切分：以 `# ` 开头的行识别为章，以 `## ` 开头的行识别为节；两个标题之间的正文合并为一个块，并附加 `book`、`part`、`chapter`、`section` 四个元数据字段。
3. 第二段处理超长块：超过 1,500 × 1.3 字符的块交由 `SentenceSplitter` 切分，块长 1,500 字符、重叠 200 字符、段落分隔符为两个换行。
4. 默认跳过「名词索引」，该部分仅为词语与页码的对照。
5. 每 50 条写入一批；collection 的 `hnsw:space` 固定为 `cosine`。

## 需要修改的位置

- `--docs` 与 `--output`：分别指定 MinerU 产物目录与 ChromaDB 输出目录。
- `DASHSCOPE_API_KEY`：从环境变量读取，代码中仅保存变量名。
- `--book` 与 `--collection`：`--book` 为教材名，`--collection` 为 ChromaDB collection 名（默认取 `--book` 的值）；collection 名为手工指定的英文名（如 `Principle_of_Biochemistry_Yang_RongWu`），更换教材时需要修改。

## 已知行为

- `chapter` 字段在 PDF 转 Markdown 后存在大范围错位：标题级别在转换时被压缩，脚本中「`# ` 为章」的假设在转换后的教材上不成立。修正方法见 `04-locating/`；不要直接修改本脚本中的字段，因为 `chapter` 与 `section` 是 `chunk_id` 的哈希输入。
- 除名词索引外，目录页（如分册教材的前置目录）也会入库，检索时可能命中。该行为可以接受；用正则判断目录页的成本更高。
