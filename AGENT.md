# AGENT.md — LearnNest Agent 入口

这是给维护 Agent 的快速入口。完整且权威的工程合同在 [AGENTS.md](AGENTS.md)；修改代码前必须阅读它与 [README.md](README.md)。`AGENTS.md` 保留是为了兼容常见 Agent 工具的自动发现规则，两个文件不应分别维护同一份详细说明。

## 项目一句话

语栖（LearnNest）是 Windows 上本地优先的 CLI：把本地学习视频或公开 URL 处理为字幕、帧、OCR、证据、`content_pack.json` 和可追溯 Markdown；笔记、播客稿和 TTS 都只能由显式命令触发。

## 先看哪里

| 目标 | 位置 |
| --- | --- |
| 人类快速上手、命令与边界 | [README.md](README.md) |
| 不可破坏合同、环境变量与完整模块地图 | [AGENTS.md](AGENTS.md) |
| CLI 与付费调用边界 | `src/learnnest/cli.py` |
| 确定性管线与 provider 子进程 | `pipeline.py`、`stages.py`、`worker.py` |
| 笔记、播客与 TTS | `note_*.py`、`podcast_*.py`、`tts_*.py` |
| 任务事实与调度/锁 | `models.py`、`task_store.py`、`scheduler.py`、`locks.py` |
| 运行时配置白名单 | `runtime_config.py` 与 [`.env.example`](.env.example) |

## 首要规则

- 固定 Python 3.12；使用 `uv run`，不要用全局 `py`。
- `.env`、密钥、Cookie、媒体、模型、原始 provider 请求和本机运行产物永不提交。
- `content_pack.json` 和稳定 `evidence_id` 是来源合同；LLM 不得伪造来源、时间戳或链接。
- `process`、`run`、`queue run`、`recover` 不得暗中发起付费 note、podcast 或 TTS 调用。
- V2/V3 bundle 只兼容消费，不自动迁移；V4 的 `source_valid` 不等于人工质量验收通过。
- `.learnnest` 是新版状态目录。切换同一输出根目录前，停止旧版进程。

运行 `uv run pytest -q`、`uv run ruff check src tests` 与 `uv run ruff format --check src tests` 验证改动。`learnnest doctor` 还需要 `src/learnnest/fixtures/README.md` 所述的本地 smoke media；它们刻意不随公开仓分发。
