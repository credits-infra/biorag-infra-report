# MinerU 接入指南

OCR 这一层不需要自己再造。MinerU 官方维护了一个生态仓库 [MinerU-Ecosystem](https://github.com/opendatalab/MinerU-Ecosystem)，把网页入口、命令行、三种语言的 SDK、MCP 服务以及 LangChain 与 LlamaIndex 的集成放在一处。把仓库地址交给你的 Agent，让它按任务自己选门。

这份指南只做一件事：说明哪个入口对应哪种任务、上限是多少、凭证放在哪里。解析本身交给 MinerU，切分与入库才是本仓库脚本的职责。

## 四种访问方式

| 入口 | 适用场景 | 前置条件 | 单文件上限 |
|---|---|---|---|
| 网页 | 试一次、看效果 | 浏览器 | 200 MB / 200 页 |
| 命令行 `mineru-open-api` | 终端里逐个或批量处理 | 运行安装脚本；精确模式需先登录 | 精确模式 200 MB / 200 页，flash 模式 10 MB / 20 页 |
| SDK（Python、Go、TypeScript） | 写进自己的流水线 | `pip install mineru-open-sdk`、`go get`、npm | 同上 |
| MCP 服务 `mineru-open-mcp` | 让 Agent 在对话里直接解析文件 | `uvx`；精确模式需 token | 同上 |

网页入口：<https://mineru.net/OpenSourceTools/Extractor>。

## 两种 API 模式

| 项目 | Precision Extract | Quick Parse（flash） |
|---|---|---|
| 鉴权 | 需要 token | 不需要，按 IP 限流 |
| 模型 | pipeline（默认）、vlm（推荐）、MinerU-HTML | 固定的轻量 pipeline |
| 文件大小 | ≤ 200 MB | ≤ 10 MB |
| 页数 | ≤ 200 页 | ≤ 20 页 |
| 批量 | 支持，单次 ≤ 200 个文件 | 仅单文件 |
| 输出 | Markdown、JSON、Zip，可导出 DOCX、HTML、LaTeX | 仅 Markdown |

精确模式另有每日额度：每个账号 1,000 页走最高优先级，超出部分优先级下降。单文件超过 200 页或 200 MB 会被拒绝，接口返回错误码 -60006，提示拆分后重试。

## 给 Agent 的最短路径

MCP 配置（`claude_desktop_config.json` 或 `.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "mineru": {
      "command": "uvx",
      "args": ["mineru-open-mcp"],
      "env": { "MINERU_API_TOKEN": "在这里填你的 token" }
    }
  }
}
```

MCP 服务暴露三个工具：`parse_documents`（PDF、DOCX、PPTX、图片、HTML 转 Markdown）、`get_ocr_languages`（列出 109 种 OCR 语言）、`clean_logs`。环境变量除 `MINERU_API_TOKEN` 外还有 `OUTPUT_DIR`（默认 `~/mineru-downloads`）、`ENABLE_LOG`、`MINERU_LOG_DIR`。Web 类 MCP 客户端可用 `mineru-open-mcp --transport streamable-http --port 8001`，连接地址为 `http://127.0.0.1:8001/mcp`。

命令行三件事：

```bash
mineru-open-api flash-extract 预览.pdf          # 不需要 token，适合快速预览
mineru-open-api auth                            # 精确模式首次登录
mineru-open-api extract 上册.pdf -o ./out/      # 精确解析，输出目录带上图片
```

批量处理与分册配合：先把大书按章或篇切成 200 页以内的 PDF（做法见本目录 README 的分册说明），再把文件清单交给命令行：

```bash
mineru-open-api extract --list filelist.txt -o ./out/
```

## 与下游的接口

MinerU 的输出是一个目录，里面有 `full.md`、`content_list.json`、`layout.json` 与 `images/`。这正是本仓库 `ingest.py` 的输入：把 `--docs` 指向这些目录即可切分入库。换成 SDK 或 MCP 只改变获取方式，输出结构不变，下游不用改。

## 凭证与额度

token 只放环境变量 `MINERU_API_TOKEN`，不要写进仓库或配置文件。flash 模式不需要 token，代价是单文件 10 MB、20 页，且按 IP 限流。教材普遍在 200 页以上，实测 7 本教材共 5,127 页，按章与篇切分后拆成 39 个任务，单个任务最大 200 页。

## 我们自己的做法

现有流水线走在线接口提交并取回结果，分册按章与篇的边界切。生态仓库提供了更省事的路径，新项目可以直接用 CLI 或 MCP，省掉自己封装提交、轮询与取回的工作。
