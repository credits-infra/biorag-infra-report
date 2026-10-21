# 04 层级定位

使用一维有序区间树与二分查询，回答两个问题：某个行号属于教材的哪一章、哪一节；某个 chunk 位于原文的哪一行。生成端与查询端都在这个目录。

| 文件 | 作用 |
|---|---|
| `build_toc_index.py` | 生成端：从 `full.md` 重建层级，产出 `toc_index.json` 与 `chunk_loc.json` |
| `toc_query.py` | 查询端：按 `chunk_id` 或行号查出处，只读两个 JSON |
| `test_build_toc_index.py` | 34 个单元测试，只用标准库，不碰 ChromaDB |
| `book_map.example.json` | collection 到教材目录的映射样例 |

## 用法

```
# 生成（同时写 toc_index.json 与 chunk_loc.json）
python3 build_toc_index.py \
    --docs   /srv/bio-kb/docs \
    --chroma /srv/bio-kb/chroma_db \
    --book-map book_map.example.json \
    --out    /srv/bio-kb/toc_index

# 只统计不写文件
python3 build_toc_index.py --docs /srv/bio-kb/docs --chroma /srv/bio-kb/chroma_db \
    --book-map book_map.example.json --dry-run

# 查询
python3 toc_query.py --index /srv/bio-kb/toc_index chunk Principle_of_Bio_9f3a1c0b77d2
python3 toc_query.py --index /srv/bio-kb/toc_index line 生物化学原理 5021
python3 toc_query.py --index /srv/bio-kb/toc_index tree 生物化学原理

# 测试
python3 -m unittest discover -s . -p 'test_*.py'
```

生成过程只读：全程只对 ChromaDB 调 `get`，不写一个字节。省略 `--chroma` 时只建树、不定位块。

## 术语

本文用到的三个词按以下含义使用。collection 指向量库中一组同源的向量，本项目一本教材对应一个 collection。块指入库后的一个文本片段，即一次检索返回的单位。`chunk_id` 是检索工具为该块生成的稳定标识，由 collection 名与内容哈希组成。

## 处理流程

PDF 转 Markdown 会把 h1 到 h4 压成同一级，入库脚本里「`# ` 是章、`## ` 是节」的假设不成立，于是 chroma 的 `chapter` 元数据大面积错位；而 `chunk_id` 是 `chapter`、`section` 的哈希输入，改元数据会让既有引用全部失效。

生成端因此不动数据库：它从原文重建层级，把结果放进两个离线文件，检索侧查一下即可。分三步。

第一步，抽标题并分类。关键在于先把「栏目」排除出层级：关键概念、综述文献、小结、参考文献、考试要求、习题、图注、题型名均不属于章或节。实测排除后全库未识别率从 25% 降到 2.0%。分类按优先级排列，栏目优先于层级，一级标题兜底为章（教材里有「第一篇 X」「VII X」「15 章名」这些不带「章」字的写法）。

第二步，建有序区间树。节点是 `{text, kind, level, start, end, parent}`，按起始行号升序存为数组，`starts` 单独保存供 `bisect` 使用。建树使用栈：新节点进入时，将栈中级别不低于它的节点弹出并闭合区间（`end` 设为新行号减一），此时栈顶为父节点。查询时 `bisect_right(starts, line) - 1` 得到的节点必然包含该行，沿 `parent` 上溯得到完整标题链。

第三步，把块对到行号。不能用标题文本查表，「小结」这类标题出现十几次，会把所有块指到第一次出现的位置。做法是拿块自身的正文前四行当锚点，从长到短试着在本书的 `full.md` 里 `find`，命中后把字符位置换成行号。

## 输出格式

`toc_index.json`：每本教材一个条目数组，一个分册一条。

```json
{
  "生物化学": [
    {
      "doc": "/srv/bio-kb/docs/生物化学/上册_5-9.pdf-<uuid>/full.md",
      "n_lines": 12480,
      "nodes": [
        {"text": "第5章 蛋白质的结构", "kind": "章", "level": 1, "start": 1, "end": 3200, "parent": null},
        {"text": "一、肽键与肽链", "kind": "节", "level": 2, "start": 41, "end": 380, "parent": 0}
      ],
      "starts": [1, 41]
    }
  ]
}
```

`kind` 取值为 `章`、`节`、`小节`、`小点`、`栏目`、`未识别`，对应 `level` 1 到 6。`parent` 是节点数组下标，根节点为 `null`。

`chunk_loc.json`：`chunk_id` 到行号与标题链。

```json
{
  "Principle_of_Bio_9f3a1c0b77d2": {
    "chunk_id": "Principle_of_Bio_9f3a1c0b77d2",
    "uuid": "<chroma 内部 id>",
    "collection": "Principle_of_Biochemistry_Yang_RongWu",
    "book": "生物化学原理",
    "doc": "生物化学原理_19-31.pdf-<uuid>",
    "line": 5021,
    "chain": [{"text": "第三篇 代谢生物化学", "kind": "章", "level": 1},
              {"text": "第19章 糖代谢", "kind": "章", "level": 1}],
    "old_chapter": "第1章 生命的分子基础"
  }
}
```

`chunk_id` 与检索工具同源：collection 前 16 字符加 `sha1(collection|chapter|section|text[:200])` 前 12 位。`old_chapter` 是原库的错值，留着便于对照。

## 需要修改的位置

- `book_map.example.json`：collection 名与 `docs/` 下目录名的映射。库里 `book` 元数据常常等于 collection 名，与目录名不同，对不上时要用映射。真题库需要另一套（`612-Y1996` 到 `612真题` 这类）。
- `--docs`、`--chroma`、`--out` 三个路径。
- 依赖：生成端需要 `chromadb`（只读），查询端与测试只用标准库。

## 验收结果

在同一份数据上与 2026-10-13 生成的索引对比（10 本书、39 个 MinerU 任务、9,986 个块）。

| 指标 | 生成器输出 |
|---|---|
| 树节点总数 | 8,656（旧索引 8,656） |
| 未识别率 | 2.0%（旧索引 2.9%） |
| 唯一 `chunk_id` | 9,672（旧索引 9,672） |
| 行号与旧索引一致 | 9,642 / 9,672，99.69% |
| 未定位的块 | 0 |
| 全文耗时 | 12 秒 |

行号不一致的 30 条全部是「同一本书内换了分册」：分册页码范围重叠（数据里存在 `10-18` 与 `11-18` 并存的重复上传）时同一段文字在多份文档里命中，两个答案都成立。生成器对此记 `ambiguous`，共 1,138 条（11.8%），并按文件名排序取第一份，保证同一输入永远得到同一输出。歧义集中在「习题」「主要参考书目」「关键概念」这类每册都出现的栏目块上，要根治得先去掉重复分册。

## 已知行为与限制

截至 2026-10-21，该索引仅作为离线数据存在，检索工具尚未将 `location` 加入返回值。合并方式与代价见技术报告 2.6 节。

- 章号与裸章名分两行的排版（`# 第8章` 换行 `# 微生物的遗传`）合并成「第8章 微生物的遗传」；下一行本身是完整章标题或栏目名时不合并。
- 完全没有「章」字、也没有编号的裸章标题（Lewin 的部分章节）靠「一级标题默认是章」兜底；某本书若把节也写成一级标题，需要按书补规则。
- 真题卷的 612 部分有 12.2% 未识别，原因是卷面本身格式自由（人工回忆版），没有稳定标题层级，引用到「年」一级即可。
- `chunk_id` 有天然碰撞：前 200 字与章节名都相同的两块会算出同一个 id，9,986 个块落到 9,672 条。这是原设计固有的，查询端遇到碰撞取第一条。
- 改完代码跑测试前要清 `__pycache__`：文件大小相同且 mtime 落在同一秒时，Python 会复用旧字节码，表现为「还原后测试仍然失败」。
