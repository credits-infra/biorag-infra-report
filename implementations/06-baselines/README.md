# 06 基线数据

`baselines.csv` 是技术报告第 4 节的机器可读版本。各列含义如下。

| 列 | 含义 |
|---|---|
| `date` | 测试日期 |
| `task` | 任务标识（`lipid_judge` 脂代谢判断题筛选；`rag_eukreg` 真核基因表达调控五层面检索生成；`exam_pipeline` 命题流水线） |
| `model` | 模型 ID |
| `config` | 配置档位（`raw` 原始任务书；`method` 加小步任务书；`none` 关闭思考；`medium` 开启思考；`with_hint` 带提示） |
| `elapsed_s` | 墙钟耗时（秒） |
| `entries` | 产出的条目数（筛选任务为命中题数） |
| `gt` | 该任务的 ground truth（筛选任务为 25 题或 24 题，见报告正文说明） |
| `note` | 备注 |

原始产物（任务书、GT 清单、模型报告、transcript）未开源，因为任务书包含真题原文。
