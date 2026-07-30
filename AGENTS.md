# AGENTS.md — LearnNest 工程维护手册

本文件面向维护代码的 Agent。先读本文件和 [README.md](README.md)，再以当前代码、测试、命令和真实运行结果判断实现事实。若本机存在 `LOCAL-RECORDS.md`，它只负责把计划、State、报告和验收记录路由到私有资料仓；公开仓必须能够独立构建、测试和维护。

## 产品与阶段

LearnNest（语栖）是 Windows 上本地优先的个人学习产品，目标是稳定地把视频内容产出为完整 Markdown 笔记，并按需产出可播放的播客音频。核心路线已经跑通，当前阶段是产品可用性收敛，不是继续扩张 MVP 功能。

- 普通用户入口以本地 WebUI 为主；CLI/API 保留给维护、自动化和高级使用。
- 当前只做稳定笔记与播客音频，不增加学习进度、卡片、统计、云服务、安装器、托盘或系统服务。
- Windows 一键启动先使用 `.bat`；不要提前引入 EXE 打包或完整 Windows 调度方案。
- Python 固定为 3.12。使用 `uv run` 或 `.venv\Scripts\python.exe`，不要使用全局 `py`。

## 目标模块边界

```text
来源适配
  → 确定性材料管线
  → evidence / content_pack
  → 笔记策略
  → 标准笔记合同
  → 播客稿 / speech.txt
  → TTS
  → 应用服务
  → WebUI / CLI
```

- `content_pack.json`、稳定 `evidence_id`、任务事实和通用发布/路径安全属于共享基础，不属于 V4。
- WebUI 调用应用服务；不得直接拼接底层 CLI、Provider 凭据或各策略私有文件。
- ASR、OCR、LLM、TTS 按能力注册 Provider；笔记和播客按工作流职责引用一个 LLM 连接。
- 笔记、播客稿和 TTS 是三个可独立配置、失败和重跑的阶段。TTS 只消费标准 `speech.txt`，不得解析 V4 私有 `note.json`。

## 笔记路线

- `assisted-note`（Writer + Reviewer）是后续唯一默认效率模式。
- `quality-note` 是独立的质量模式，当前仍处于开发调试且成功率低，不进入 WebUI 稳定承诺。
- V4 是已废弃路线。当前仓库仍含 V4 实现和兼容依赖，清理前必须先画出依赖图，区分共享基础与 V4 私有合同；不得继续给 V4 增加功能。
- 历史 V2/V3/V4 产物只按确有需要保留只读消费或迁移工具，不得反向污染新标准合同。
- `source_valid`、`model_reviewed`、机器门禁、人工阅读和图片验收是不同结论，不能互相替代。

## Provider 与自动化

产品稳定承诺只收敛到：

- LLM：MiMo、DeepSeek。
- TTS：Windows 系统 TTS（默认）、MiMo TTS（可选云端）。

保留新增 Provider 的接口，但不要实现或宣传其他模型适配。Provider 不自动跨供应商回退。

当前代码仍存在历史耦合：`MIMO_API_KEY` 被笔记、播客和 TTS 共用，自动化也会从同一配置构造相关 Provider；这是一项待清理债务，不是目标设计。新代码不得加深耦合：

- 各能力配置、凭据、预算、状态分别管理。
- 输出根保存默认职责绑定；任务开始时冻结绑定，不提供任务级临时覆盖。
- 自动工作流默认关闭，必须由用户显式开启。
- 可配置重试 0–3 次，默认重试 1 次；只重试明确的临时错误。
- 每次尝试都可见、计数并持久化；`unknown`、永久错误和配置错误立即停止。
- Provider、云 TTS、重试次数或自动来源范围等实质变化会使旧授权失效。

## 当前代码地图

| 区域 | 主要位置 | 当前职责 |
| --- | --- | --- |
| CLI 与命令边界 | `src/learnnest/cli.py` | Typer 命令、显式付费门槛、错误脱敏 |
| WebUI 与应用入口 | `src/learnnest/web_app.py`、相关 service 模块 | 本地页面、来源、任务与成品视图 |
| 确定性管线 | `pipeline.py`、`stages.py`、`worker.py` | 字幕、帧、OCR、evidence、内容包与重跑 |
| 任务与批次事实 | `models.py`、`task_store.py`、`batch_*.py` | 稳定 task ID、JSON 事实、可恢复执行 |
| 笔记 | `note_*.py`、`evidence_*.py`、`quality_*.py`、`rendering.py` | 历史严格路线、效率/质量策略、验证和渲染 |
| 播客与音频 | `podcast_*.py`、`tts_*.py` | 播客稿、speech、音频与发布 |
| 来源与下载 | `sources.py`、`downloader.py`、`adapters/` | 本地输入、公开 URL、抖音发现与下载 |
| 调度与锁 | `scheduler.py`、`locks.py`、`schedule_*.py` | 前台 tick、资源 semaphore 与跨进程锁 |
| 运行配置 | `runtime_config.py`、`.env.example` | 当前白名单和历史兼容配置 |
| 回归保护 | `tests/` | 行为合同、失败路径和 Provider fake |

文件名和模块会随 V4 清理调整。改动前用 `rg` 定位真实调用链，不用此表替代代码核验。

## 不可破坏的工程合同

- `process`、`run`、`queue run`、`recover` 不得隐式调用付费 note、podcast 或 TTS Provider。
- `task_id` 是身份，标题和目录名不是；TaskRecord/BatchManifest JSON 是事实，SQLite 是可重建投影。
- OCR、字幕和 AI 补充内容的来源类型必须区分；URL 只能逐字来自被引用的 transcript 或 OCR。
- ASR 与 OCR 独立子进程运行；Paddle CPU OCR 显式禁用 MKLDNN。
- LLM 输出必须受约束并由程序验证 evidence、OCR 父 frame、URL、SHA 与最终 Markdown。
- 外部调用成功响应先完整落盘，再验证和发布；恢复优先复用已落盘响应，不隐藏重复调用。
- API Key、Cookie、原始 Provider 请求、模型缓存和本地媒体不得写入 Git、任务、批次、日志或 SQLite。
- `.learnnest` 是当前运行状态目录。切换同一输出根前先停止旧版本进程。

## 文档与 Skill 产物

公开仓只保留：

- 面向用户的 `README.md`；
- 面向维护者的 `AGENTS.md` / `AGENT.md`；
- 与代码紧邻、对构建测试必要的文档。

产品路线、PRD、实施计划、任务 State、调查报告、验收记录和分支/worktree 产物不得放在公开仓根目录。若存在 `LOCAL-RECORDS.md`，按其中路径移交私有资料仓；不存在时，先在会话中交付，不为满足格式创建本地依赖。

所有 Skill（包括 `leader`）遵守同一规则：

- `leader` 任务书默认留在会话；明确要求保存时进入资料仓 `docs/plans/`。
- 跨会话执行只维护资料仓中的一份 `STATE.md`，进度和阻塞不得拆成公开仓 `PROGRESS.md` / `BLOCKED.md`。
- 功能分支或 worktree 产生的 Plan、State、Report、任务书、验收文档及其他 Markdown/JSON 也属于分支产物，必须移交资料仓。
- 仍指导全项目的文档按 Plan/State/Report 职责落位；只对该分支有意义的文档进入对应记录的 `facts/documents/`，不得两处复制。
- 分支产物文档头和 `record.md` 至少标注来源仓库、`branch@sha`、worktree 逻辑名称、产物类型、结果强度和未验证项；不提交本机 worktree 绝对路径。
- 任务完成后更新稳定入口并归档 Plan/State，不让过程文档继续冒充当前事实。

## 修改与验证

先区分症状、触发条件和根因，再做最小因果范围修改。V4 清理和 Provider 解耦必须先证明哪些类型、字段和文件是共享合同，不能按文件名前缀批量删除。

```powershell
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run learnnest --help
uv run learnnest doctor
git diff --check
```

`doctor` 依赖本机 ASR/OCR 环境和 `src/learnnest/fixtures/README.md` 所述 fixture，但不应下载模型或产生付费调用。真实视频、笔记、播客和音频验收使用新的输出根，并分别说明机器、阅读、图片和听音结果。

提交、推送和创建 PR 前必须完成匹配验证并等待用户明确要求。只暂存本次任务范围。
