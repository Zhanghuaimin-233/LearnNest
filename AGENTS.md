# AGENTS.md — LearnNest 工程维护手册

本文件面向维护代码的 Agent，不是产品历史或验收资料库。先读本文件和 [README.md](README.md)，再按当前代码、测试与命令行为做判断；本地私有资料仓仅在存在 `LOCAL-RECORDS.md` 时作为补充参考，绝不是构建或测试前提。

## 项目与运行边界

LearnNest（语栖）是本地优先的 Windows CLI：本地视频或公开 URL 经过字幕、帧、OCR、证据和 `content_pack.json`，形成可追溯 Markdown 笔记；笔记、播客稿和 TTS 均为显式可选步骤。

- Python 固定为 3.12。使用 `uv run` 或 `.venv\Scripts\python.exe`，不要使用全局 `py`。
- `process`、`run`、`queue run`、`recover` 不得隐式调用付费 note、podcast 或 tts provider。
- API Key、Cookie、原始 provider 请求、模型缓存和本地媒体不得写入 Git、任务、批次、日志或 SQLite。
- `content_pack.json` 是 LLM / 外部 Agent 的标准输入；每个可引用事实必须保留稳定 `evidence_id`。
- LLM 只能生成受约束结构化内容；质量优先 Writer 的正文位于 JSON `markdown` 字段中。程序负责验证 evidence、OCR 父 frame、URL、SHA、视觉预算与最终 Markdown。
- 新版本的运行状态目录是 `.learnnest`。切换同一输出根目录前先停止旧版进程：新旧版本的锁目录不同，不能依靠锁来避免并发写入。

## 模块地图

| 区域 | 主要文件 | 职责 |
| --- | --- | --- |
| CLI 与命令边界 | `src/learnnest/cli.py` | Typer 命令、显式付费门槛、错误脱敏。 |
| 确定性管线 | `pipeline.py`、`stages.py`、`worker.py` | 字幕、帧、OCR、evidence、内容包与重跑。 |
| 任务与批次事实 | `models.py`、`task_store.py`、`batch_*.py` | 稳定 task ID、JSON 事实、可恢复执行。 |
| 笔记链路 | `note_*.py`、`rendering.py` | V4 模板、provider、证据验证、Markdown 渲染。 |
| 读者优先笔记 | `evidence_*.py`、`quality_*.py` | 相关性蒸馏、Writer 合同、provenance、质量门禁与可恢复发布。 |
| 播客与音频 | `podcast_*.py`、`tts_*.py` | 显式生成、SHA/ownership 校验、发布。 |
| 来源与下载 | `sources.py`、`downloader.py`、`adapters/` | 本地输入、公开 URL、抖音发现与下载。 |
| 调度与锁 | `scheduler.py`、`locks.py`、`schedule_*.py` | 前台 tick、资源 semaphore 与跨进程锁。 |
| 运行配置 | `runtime_config.py`、`.env.example` | 仅白名单运行时环境变量。 |
| 回归保护 | `tests/` | 行为合同、失败路径和 provider fake。 |

## 配置文件与环境变量

`.env` 是本机便利配置，永远不提交。以 [`.env.example`](.env.example) 为准；`runtime_config.py` 是可从 `.env` 读取的白名单唯一来源。

- `MIMO_API_KEY`：显式 MiMo note / podcast / TTS。
- `LEARNNEST_NOTE_API_KEY`、`LEARNNEST_NOTE_BASE_URL`、`LEARNNEST_NOTE_MODEL`：兼容 OpenAI 的笔记 provider，三项必须同时存在，且不能与 `MIMO_API_KEY` 共用。
- `LEARNNEST_NOTE_PROVIDER`、`LEARNNEST_NOTE_JSON_MODE`、`LEARNNEST_NOTE_SAFE_INPUT_TOKENS`：可选 provider 参数。
- `HUGGINGFACE_HUB_CACHE`：本机 ASR cache。
- `DOUYIN_COOKIE`：仅 adapter 边界运行时读取。

## 不可破坏的合同

- V4 新笔记仅允许完整内容包 + 受限模板；默认一次模型调用，`report` 与 `gate` 的审验语义不得混淆 `source_valid` 与人工质量验收。
- 质量优先链路必须将读者 Markdown 与完整证据闭包分离：Writer 只接收 `core/supporting` unit，读者侧每个实质正文块最多一个紧凑脚注，完整闭包写入 `note.provenance.json`。
- 所有 `core/supporting` unit 必须至少在一个实质正文块中被引用；摘要、脉络、实践、注意事项和复习只有在不引入新事实时才能作为 `derived_from_cited_note` 省略重复引用。
- 图片只能来自 Writer 显式选择且程序验证过的视觉 unit，单篇最多三张；候选、active 和交付 Markdown 必须按各自目录重新渲染相对路径。
- Writer HTTP 成功响应必须在校验前本地落盘。只有同源 organization 的 SHA 复用和已落盘响应的本地 `recover` 可以避免重复付费调用；不得篡改失败计划、隐藏重试或修剪未知来源。
- V2/V3 bundle 仅保留 validate / rerender / podcast / tts 的兼容消费，不自动迁移或重新分类。
- `task_id` 是身份，标题和目录名不是；TaskRecord / BatchManifest JSON 是事实，SQLite 是可重建投影。
- OCR、字幕、AI 补充内容的来源类型必须区分；URL 只能逐字来自被引用的 transcript 或 OCR。
- ASR 与 OCR 必须独立子进程运行；Paddle CPU OCR 显式禁用 MKLDNN。
- `schedule tick` 仅是前台命令，不能修改 Windows Task Scheduler；单槽资源必须同时持有进程内 semaphore 与跨进程 OS 锁。

## 修改与验证

先定位触发条件和根因，再做最小因果范围改动。新增确定性规则或修复可复现缺陷时，为行为添加覆盖；不要借重构混入无关改动。

```powershell
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run learnnest --help
uv run learnnest doctor
git diff --check
```

`doctor` 依赖本机 ASR / OCR 可用环境及 `src/learnnest/fixtures/README.md` 所述的本地权利明确 fixture，但不应下载模型或产生付费调用。真实视频、笔记、播客和音频验收必须使用新的输出根目录，并单独说明是否完成人工阅读、图片与音频检查。

提交、推送和创建 PR 前必须先完成匹配验证，并等待用户明确要求。只暂存本次任务范围内的文件。
