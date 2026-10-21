#!/usr/bin/env python3
"""build_toc_index.py 的单元测试。

跑法：
    python3 -m unittest discover -s implementations/04-locating -p 'test_*.py' -v

测试只用标准库，不碰 ChromaDB、不联网。真实数据的验收在 README 的「验收」一节。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_toc_index as b  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_篇与部分都算一级(self):
        for text in ("第一篇 结构生物化学", "第2部分 细胞"):
            self.assertEqual(b.classify(text, 1), (b.KIND_章, b.LEVEL_章))

    def test_章匹配中文与阿拉伯数字(self):
        for text in ("第三章 蛋白质的结构", "第8章 微生物的遗传", "第 12 章 核酸"):
            self.assertEqual(b.classify(text, 2), (b.KIND_章, b.LEVEL_章))

    def test_一级标题默认是章(self):
        # Lewin 的章是裸标题，没有「章」字，靠级别兜底
        self.assertEqual(b.classify("基因是 DNA", 1), (b.KIND_章, b.LEVEL_章))

    def test_节的三类写法(self):
        self.assertEqual(b.classify("第一节 遗传学发展历史", 2), (b.KIND_节, b.LEVEL_节))
        self.assertEqual(b.classify("一、遗传学代表性研究成果", 2), (b.KIND_节, b.LEVEL_节))
        self.assertEqual(b.classify("15.1 细菌的细胞结构", 2), (b.KIND_节, b.LEVEL_节))

    def test_三级数字算小节(self):
        self.assertEqual(b.classify("15.1.1 细菌的形态", 2), (b.KIND_小节, b.LEVEL_小节))
        self.assertEqual(b.classify("（三）己糖激酶的调节", 2), (b.KIND_小节, b.LEVEL_小节))
        self.assertEqual(b.classify("(1) 第一点", 2), (b.KIND_小节, b.LEVEL_小节))

    def test_小点(self):
        self.assertEqual(b.classify("①细胞膜的流动性", 2), (b.KIND_小点, b.LEVEL_小点))
        self.assertEqual(b.classify("1. Griffith 的转化实验", 2), (b.KIND_小点, b.LEVEL_小点))

    def test_栏目优先于层级(self):
        cases = [
            "参考文献：",
            "小结",
            "关键概念",
            "二、选择题：20 分。答错不倒扣分。",
            "图 15.5 羧化体的结构和功能",
            "←图 15.5 羧化体的结构和功能",
            "中科院 1996 生化与分子",
            "A",
        ]
        for text in cases:
            self.assertEqual(b.classify(text, 2), (b.KIND_栏目, b.LEVEL_栏目), text)

    def test_卷头一级算章二级算栏目(self):
        # 大纲与真题的首行是文档容器，当章才能引用「某年某题型」
        self.assertEqual(b.classify("中国科学院大学硕士研究生入学考试   《生物化学》考试大纲", 1),
                         (b.KIND_章, b.LEVEL_章))
        self.assertEqual(b.classify("中科院 1996 生化与分子", 1), (b.KIND_章, b.LEVEL_章))
        self.assertEqual(b.classify("《生物学综合》考试大纲", 2), (b.KIND_栏目, b.LEVEL_栏目))

    def test_题型在任意级别都是栏目(self):
        self.assertEqual(b.classify("一.是非题：每题1.5分", 2), (b.KIND_栏目, b.LEVEL_栏目))
        self.assertEqual(b.classify("二、选择题：20 分。答错不倒扣分。", 1), (b.KIND_栏目, b.LEVEL_栏目))

    def test_认不出来就报未识别(self):
        self.assertEqual(b.classify("物》", 2), (b.KIND_未识别, b.LEVEL_未识别))


class TestMerge(unittest.TestCase):
    def test_章号与裸章名合并(self):
        items = b.extract_headings("# 第8章\n\n# 微生物的遗传\n\n## 第一节 遗传的物质基础\n")
        merged = b.merge_chapter_numbers(items)
        self.assertEqual(merged[0][1], "第8章 微生物的遗传")
        self.assertEqual(merged[0][3], b.KIND_章)
        self.assertEqual(len(merged), 2)  # 合掉一条，剩章与第一节课

    def test_章号后面跟栏目时不吞掉(self):
        items = b.extract_headings("# 第8章\n## 参考文献：\n## 第一节 遗传的物质基础\n")
        merged = b.merge_chapter_numbers(items)
        self.assertEqual([m[1] for m in merged], ["第8章", "参考文献：", "第一节 遗传的物质基础"])

    def test_章号后面跟章标题不合并(self):
        items = b.extract_headings("# 第8章\n# 第9章 酶\n")
        merged = b.merge_chapter_numbers(items)
        self.assertEqual([m[1] for m in merged], ["第8章", "第9章 酶"])


class TestTree(unittest.TestCase):
    def _tree(self, md: str):
        return b.index_book(md)

    def test_区间收口与父子关系(self):
        md = "# 第一章 绪论\n\n## 第一节 甲\n\n## 第二节 乙\n\n# 第二章 酶\n\n## 第一节 丙\n"
        entry = self._tree(md)
        nodes = entry["nodes"]
        self.assertEqual([n.kind for n in nodes], [b.KIND_章, b.KIND_节, b.KIND_节, b.KIND_章, b.KIND_节])
        self.assertEqual([n.level for n in nodes], [1, 2, 2, 1, 2])
        self.assertEqual([n.parent for n in nodes], [None, 0, 0, None, 3])
        # 每章的结束行是下一章的前一行
        self.assertEqual(nodes[0].end, nodes[3].start - 1)
        self.assertEqual(nodes[1].end, nodes[2].start - 1)
        # 最后一个节点收到文末
        self.assertEqual(nodes[4].end, entry["n_lines"])

    def test_栏目挂在最近的上级之下(self):
        md = "# 第一章 绪论\n\n## 小结\n\n## 第一节 甲\n"
        nodes = self._tree(md)["nodes"]
        self.assertEqual(nodes[1].kind, b.KIND_栏目)
        self.assertEqual(nodes[1].parent, 0)
        self.assertEqual(nodes[2].parent, 0)
        self.assertEqual(nodes[1].end, nodes[2].start - 1)

    def test_点查询命中边界(self):
        md = "# 第一章 绪论\n\n## 第一节 甲\n\n## 第二节 乙\n"
        entry = self._tree(md)
        nodes, starts = entry["nodes"], entry["starts"]
        self.assertEqual([n.text for n in b.chain_at(nodes, starts, 1)], ["第一章 绪论"])
        self.assertEqual([n.text for n in b.chain_at(nodes, starts, 3)], ["第一章 绪论", "第一节 甲"])
        self.assertEqual([n.text for n in b.chain_at(nodes, starts, 3)], ["第一章 绪论", "第一节 甲"])
        last = entry["n_lines"]
        self.assertEqual(b.chain_at(nodes, starts, last)[-1].text, "第二节 乙")

    def test_空文档不炸(self):
        entry = self._tree("")
        self.assertEqual(entry["nodes"], [])


class TestLocate(unittest.TestCase):
    FULL = (
        "# 第一章 绪论\n"      # 1
        "\n"                    # 2
        "## 小结\n"             # 3
        "\n"                    # 4
        "第一处小结正文。\n"      # 5
        "\n"                    # 6
        "## 小结\n"             # 7
        "\n"                    # 8
        "第二处小结正文。\n"      # 9
    )

    def test_重复标题不误定位(self):
        line = b.locate_chunk(self.FULL, "第二处小结正文。\n后面还有一行")
        self.assertEqual(line, 9)

    def test_单行锚点退化(self):
        # 前两行不在原文里，退回首行
        line = b.locate_chunk(self.FULL, "第一处小结正文。\n原文里没有这一行")
        self.assertEqual(line, 5)

    def test_找不到返回None(self):
        self.assertIsNone(b.locate_chunk(self.FULL, "完全不相干的文字"))


class TestChunkId(unittest.TestCase):
    # 这三个值与生产检索工具 kb_search.make_chunk_id 的输出逐字一致
    GOLDEN = [
        (("General_Biology", "第1章 绪论", "一、生物学", "细胞的发现与研究"), "General_Biology_98f86a50008c"),
        (("612-Biochemistry-and-Molecular-Biology-Guideline", "", "", "中国科学院大学"), "612-Biochemistry_d7ccb1ea8d9a"),
        (("Demo Book", "ch", "sec", "x" * 300), "Demo_Book_f8861c994ebe"),
    ]

    def test_与生产工具一致(self):
        for args, expect in self.GOLDEN:
            self.assertEqual(b.make_chunk_id(*args), expect)

    def test_前200字之外不影响id(self):
        a = b.make_chunk_id("C", "ch", "sec", "y" * 200 + "甲")
        b_ = b.make_chunk_id("C", "ch", "sec", "y" * 200 + "乙")
        self.assertEqual(a, b_)


class TestBuildChunkLoc(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        book = root / "示例教材"
        doc = book / "示例教材_1-2.pdf-abc"
        doc.mkdir(parents=True)
        (doc / "full.md").write_text(
            "# 第一章 绪论\n\n## 第一节 甲\n\n甲节正文第一行。\n还有第二行。\n",
            encoding="utf-8",
        )
        toc = {}
        md = (doc / "full.md").read_text(encoding="utf-8")
        entry = b.index_book(md)
        toc["示例教材"] = [{
            "doc": str(doc / "full.md"),
            "n_lines": entry["n_lines"],
            "nodes": [n.as_dict() for n in entry["nodes"]],
            "starts": entry["starts"],
        }]
        self.toc = toc

    def tearDown(self):
        self.tmp.cleanup()

    def _rec(self, text, chapter="错的章", section="错的节", collection="Demo_Col", uuid="u1"):
        return b.ChunkRecord(collection=collection, text=text,
                             metadata={"chapter": chapter, "section": section, "book": "示例教材"}, uuid=uuid)

    def test_定位与标题链(self):
        loc, stats = b.build_chunk_loc([self._rec("甲节正文第一行。\n还有第二行。")], self.toc, {})
        self.assertEqual(stats["located"], 1)
        rec = next(iter(loc.values()))
        self.assertEqual(rec["line"], 5)
        self.assertEqual([c["text"] for c in rec["chain"]], ["第一章 绪论", "第一节 甲"])
        self.assertEqual(rec["old_chapter"], "错的章")  # 原库错值保留，便于对照

    def test_同一个块重复出现只算一次碰撞(self):
        rec = self._rec("甲节正文第一行。\n还有第二行。")
        loc, stats = b.build_chunk_loc([rec, rec], self.toc, {})
        self.assertEqual(stats["located"], 1)
        self.assertEqual(stats["collisions"], 1)

    def test_元数据缺book时用映射兜底(self):
        rec = b.ChunkRecord(collection="X", text="甲节正文第一行。\n还有第二行。", metadata={}, uuid="u2")
        loc, stats = b.build_chunk_loc([rec], self.toc, {"X": "示例教材"})
        self.assertEqual(stats["located"], 1)

    def test_元数据书名是collection名时以映射为准(self):
        # 生产库里 book 元数据等于 collection 名，直接拿它当目录名会导致整库定位不到
        rec = b.ChunkRecord(collection="Demo_Col", text="甲节正文第一行。\n还有第二行。",
                            metadata={"book": "Demo_Col", "chapter": "", "section": ""}, uuid="u4")
        loc, stats = b.build_chunk_loc([rec], self.toc, {"Demo_Col": "示例教材"})
        self.assertEqual(stats["located"], 1)
        self.assertEqual(stats["no_book"], 0)
        rec_out = next(iter(loc.values()))
        self.assertEqual(rec_out["book"], "示例教材")

    def test_分册重叠时计入歧义并取排序第一份(self):
        # 两分册页码范围重叠（真实数据里存在 10-18 与 11-18 并存的重复上传）
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "示例教材"
            docs = []
            for name in ("示例教材_1-2.pdf-b", "示例教材_1-2.pdf-a"):
                d = root / name
                d.mkdir(parents=True)
                (d / "full.md").write_text("# 第一章 绪论\n\n## 第一节 甲\n\n甲节正文第一行。\n还有第二行。\n", encoding="utf-8")
                entry = b.index_book((d / "full.md").read_text(encoding="utf-8"))
                docs.append({"doc": str(d / "full.md"), "n_lines": entry["n_lines"],
                             "nodes": [n.as_dict() for n in entry["nodes"]], "starts": entry["starts"]})
            toc = {"示例教材": docs}
            rec = b.ChunkRecord(collection="C", text="甲节正文第一行。\n还有第二行。",
                                metadata={"book": "C"}, uuid="u9")
            loc, stats = b.build_chunk_loc([rec], toc, {"C": "示例教材"})
            self.assertEqual(stats["located"], 1)
            self.assertEqual(stats["ambiguous"], 1)
            # 取排序后的第一份，保证同一输入永远同一输出
            picked = next(iter(loc.values()))["doc"]
            self.assertEqual(picked, "示例教材_1-2.pdf-a")

    def test_没有对上的书计入no_book(self):
        rec = b.ChunkRecord(collection="Y", text="甲节正文第一行。", metadata={}, uuid="u3")
        loc, stats = b.build_chunk_loc([rec], self.toc, {})
        self.assertEqual(stats["no_book"], 1)
        self.assertEqual(loc, {})


class TestResolveBookMap(unittest.TestCase):
    BOOKS = ["生物化学", "普通生物学", "Microbiology"]

    def test_显式映射优先(self):
        out, unmapped = b.resolve_book_map(["C1"], self.BOOKS, {"C1": "普通生物学"}, {"C1": "生物化学"})
        self.assertEqual(out, {"C1": "普通生物学"})
        self.assertEqual(unmapped, [])

    def test_元数据书名等于目录名时直接采用(self):
        out, _ = b.resolve_book_map(["C1"], self.BOOKS, {}, {"C1": "生物化学"})
        self.assertEqual(out, {"C1": "生物化学"})

    def test_归一化后互相包含(self):
        out, _ = b.resolve_book_map(["microbiology-notes"], self.BOOKS, {}, {})
        self.assertEqual(out, {"microbiology-notes": "Microbiology"})

    def test_对不上的库要报出来(self):
        out, unmapped = b.resolve_book_map(["General_Biology"], self.BOOKS, {}, {})
        self.assertEqual(out, {})
        self.assertEqual(unmapped, ["General_Biology"])


class TestIndexBook(unittest.TestCase):
    def test_行数按编辑器口径(self):
        self.assertEqual(b.index_book("# 章\n正文")["n_lines"], 2)
        self.assertEqual(b.index_book("# 章\n正文\n")["n_lines"], 2)

    def test_输出可JSON序列化(self):
        entry = b.index_book("# 第一章 绪论\n\n## 第一节 甲\n")
        dumped = json.dumps({"nodes": [n.as_dict() for n in entry["nodes"]], "starts": entry["starts"]},
                            ensure_ascii=False)
        self.assertIn("第一章 绪论", dumped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
