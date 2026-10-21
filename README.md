# bioinfra-report

一套面向考研的个人 AI 基础设施技术报告与参考实现。

## 内容

| 文件 | 说明 |
|---|---|
| `技术报告.md` | 主文档。从 OCR 到向量数据库，再到对外服务，最后是模型能力评测 |
| `implementations/` | 分六类的脱敏参考实现，见该目录下的 `README.md` |

## 复现前提

参考实现需要 Python 3.11、ChromaDB 1.x、LlamaIndex 0.14.x、一个可用的 DashScope 账号（embedding 与 rerank），以及 MinerU 的在线接口账号（OCR 阶段）。

OCR 阶段的接入方式见 `implementations/01-ocr-chunking/MinerU-接入指南.md`，其中列出去处（官方生态仓库的网页入口、命令行、SDK 与 MCP 服务）、两种 API 模式的上限与凭证存放位置。代码中的路径均为占位路径，运行前需要改为本地路径。

## 许可

文字部分采用 CC BY 4.0（ https://creativecommons.org/licenses/by/4.0/ ）
`implementations/` 下的代码采用 MIT，见 `LICENSE`。从其他项目复制的文件保留原许可，见各目录 README。
