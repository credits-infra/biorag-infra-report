# 04 层级定位

使用一维有序区间树与二分查询，回答两个问题：某个行号属于教材的哪一章、哪一节；某个 chunk 位于原文的哪一行。

## 数据文件

两个 JSON 文件由离线脚本生成。生成脚本未随本报告开源，格式如下，可自行重建。

`toc_index.json`：每本教材一个节点数组。

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
      "starts": [1, 41, "..."]
    }
  ]
}
```

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
    "chain": [{"text": "第一篇 结构生物化学", "kind": "篇", "level": 0}, {"text": "第19章 糖代谢", "kind": "章", "level": 1}]
  }
}
```

## 查询

`toc_query.py` 是本报告附带的最小可运行实现；生产脚本未保留。它只读取两个 JSON，不访问 ChromaDB。

```
python3 toc_query.py chunk Principle_of_Bio_9f3a1c0b77d2
python3 toc_query.py line 生物化学原理 5021
python3 toc_query.py tree 生物化学原理
```

默认从当前目录读取两个数据文件，可用 `--index` 指定目录。

## 建树规则

节点按 `start` 升序存为数组，`starts` 单独保存供 `bisect` 使用。建树使用栈：新节点进入时，将栈中级别不低于它的节点弹出并闭合区间（`end` 设为新行号减一），此时栈顶为父节点。查询时，`bisect_right(starts, line) - 1` 得到的节点必然包含该行。

「栏目」必须在建树前排除：关键概念、综述文献、小结、参考文献、考试要求、习题、图注、题型名均不属于层级。实测排除后，全库未识别率从 25% 降至 2.9%。

## 已知状态

该索引目前仅作为离线数据存在，检索工具尚未将 `location` 加入返回值。合并方式与代价见技术报告 2.6 节。
