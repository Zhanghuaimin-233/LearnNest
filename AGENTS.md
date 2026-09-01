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

- `content_pack.json`、稳定 `evidence_id`、任务事实和通用发布/路径安全属于共享基础，不能随旧路线清除。
- WebUI 调用应用服务；不得直接拼接底层 CLI、Provider 凭据或各策略私有文件。
- ASR、OCR、LLM、TTS 按能力注册 Provider；笔记和播客按工作流职责引用一个 LLM 连接。
- 笔记、播客稿和 TTS 是三个可独立配置、失败和重跑的阶段。TTS 只消费已验证的 `podcast_script.json`、`speech.txt` 和播客身份，不读取笔记、`content_pack.json` 或模板。

## 笔记路线

- `assisted-note`（Writer + Reviewer）是后续唯一默认效率模式。
- `quality-note` 是独立的质量模式，当前仍处于开发调试且成功率低，不进入 WebUI 稳定承诺。
- 旧的严格生成、模板、审验和专属 CLI 已移除；清理必须逐符号进行，保留共享基础、质量模式和效率模式。
- 历史 V2/V3 产物只按确有需要保留只读消费，不得反向污染新标准合同。
- `source_valid`、`model_reviewed`、机器门禁、人工阅读和图片验收是不同结论，不能互相替代。

## Provider 与自动化

当前已经实现并完成机器验证的稳定范围是：

- LLM：MiMo、DeepSeek。
- TTS：Windows 系统 TTS（默认）、MiMo TTS（可选云端）。

W3.2 已确认可以扩展经过审查的官方 LLM Provider 预设，以及 OpenRouter 这类具有公开官方
文档的多模型平台；未实现和未验收的预设不得宣传为当前能力。新增预设只支持普通用户的固定
官方 endpoint + API Key 路径，内部声明 API 格式、官方 Key 入口、目录模式和显式允许模型列表；
当前不增加地域、Workspace、Resource、Deployment 或其他企业连接字段。不得复制 CC Switch 的
赞助商、中转站、推广链接、协议转换、候选地址探测或隐藏路由。Provider 不自动跨供应商回退。

当前实现已将连接、密钥引用、预算和失败域按能力与工作流职责拆分；`.env`
只保留显式旧 CLI 的迁移兼容入口。新代码必须维持以下合同：

- 各能力配置、凭据引用、预算和状态分别管理；笔记域连接不得跨用到 podcast 或 TTS。
- 输出根保存默认职责绑定；任务开始时冻结 binding 与 settings SHA，不提供任务级临时覆盖。
- API Key 只以 Windows CurrentUser DPAPI 密文保存；设置、任务、日志和 API 不得保存或回显明文。
- 新增 LLM 连接由用户指定连接名称；WebUI 在最终保存前按预设固定的目录模式获取模型。实时目录
  只在该次请求内使用本次 Key；内置目录不调用 Provider，也不把 Key 提前写入。用户显式选择具体
  模型后，连接、DPAPI secret 与模型一次性保存。不得静默落入预设默认模型；已保存连接继续手动
  获取目录并显式更新模型。
- 每个预设必须固定声明 `live` 或 `curated` 目录模式，不能在实时目录认证、网络或格式失败后静默
  降级。实时目录候选是 Provider 返回与当前适配器允许列表的交集；内置目录是带来源标签和 adapter
  revision 的 LearnNest 支持列表，必须提示它不证明账号已开通。实时目录请求零重试、不探测候选
  URL、不持久化原始响应，也不进入任务调用预算。
- 设置更新先校验完整候选，再写 secret 和 settings；不兼容更新必须保持旧状态不变。
- 付费整理许可默认关闭，必须由用户显式确认；它与“自动加入新收藏”是两个独立状态。手动任务只要求有效付费许可，不能被自动来源开关阻塞。
- 可配置重试 0–3 次，默认重试 1 次；只重试明确的临时错误。
- 显式命令和 automation 共用付费准入；在构造 Provider 前先持久化 running 事实。
- 全局及 note/podcast/TTS/ASR/OCR 五组任务预算都按冻结策略执行；普通任务的 running 和 unknown 继续计数。
- 普通任务的每次尝试都可见、计数并持久化；`unknown`、永久错误和配置错误立即停止。
- 设置页的云端“检查连接”是显式诊断：只接受精确付费确认，每次发送一个最小真实请求且不自动重试；它可能产生 Provider 费用，会持久化审计事实但不计入任务每日调用限额，也不保存 Provider 返回正文。本地 ASR/OCR/Windows TTS 检查只运行对应本机能力，不产生云端费用。
- Provider、云 TTS、重试次数或自动来源范围等实质变化会使旧授权失效。

## 当前代码地图

| 区域 | 主要位置 | 当前职责 |
| --- | --- | --- |
| CLI 与命令边界 | `src/learnnest/cli.py` | Typer 命令、显式付费门槛、错误脱敏 |
| WebUI 与应用入口 | `src/learnnest/web_app.py`、相关 service 模块 | 本地页面、来源、任务与成品视图 |
| 确定性管线 | `pipeline.py`、`stages.py`、`worker.py` | 字幕、帧、OCR、evidence、内容包与重跑 |
| 任务与批次事实 | `models.py`、`task_store.py`、`batch_*.py` | 稳定 task ID、JSON 事实、可恢复执行 |
| 笔记 | `note_*.py`、`evidence_*.py`、`quality_*.py`、`rendering.py` | 效率/质量策略、历史只读兼容、验证和渲染 |
| 播客与音频 | `podcast_*.py`、`tts_*.py` | 播客稿、speech、音频与发布 |
| 来源与下载 | `sources.py`、`downloader.py`、`adapters/` | 本地输入、公开 URL、抖音发现与下载 |
| 调度与锁 | `scheduler.py`、`locks.py`、`schedule_*.py` | 前台 tick、资源 semaphore 与跨进程锁 |
| Provider 与运行配置 | `provider_profiles.py`、`provider_service.py`、`provider_secrets.py`、`runtime_config.py` | 连接、密钥、职责冻结、调用准入和旧 CLI 兼容 |
| 回归保护 | `tests/` | 行为合同、失败路径和 Provider fake |

文件名和模块会随旧路线清理调整。改动前用 `rg` 定位真实调用链，不用此表替代代码核验。

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

先区分症状、触发条件和根因，再做最小因果范围修改。旧路线清理和 Provider 解耦必须先证明哪些类型、字段和文件是共享合同，不能按文件名前缀批量删除。

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
