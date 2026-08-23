<p align="center">
  <img src="assets/brand/icon.png" width="144" alt="语栖 LearnNest 图标">
</p>

<h1 align="center">语栖 · LearnNest</h1>

<p align="center">
  把本地视频和公开内容整理成可阅读的完整笔记，并按需生成可播放的播客音频。
</p>

LearnNest 是面向个人使用的本地学习产品。它先把字幕、画面和 OCR 整理成可追溯材料，再生成学习笔记；需要时，笔记还可以继续变成播客稿和音频。数据和运行产物默认留在用户指定的本地目录。

项目已经跑通“视频 → 完整笔记 → 播客音频”的核心技术路线，完成了不稳定路线收敛以及 Provider 与工作流解耦。WebUI 已作为普通用户入口进入产品闭环收口阶段，但当前版本还不能把页面可用等同于完整日常流程已经验收。

## 当前能做什么

| 能力 | 当前状态 |
| --- | --- |
| 本地视频 | 支持单个文件、目录和可恢复任务处理 |
| 公开 URL | 通过本机 `yt-dlp` 尽力下载视频与平台字幕 |
| 抖音收藏 | 支持隔离官方窗口登录、默认收藏同步、待下载队列与本地缩略图 |
| 材料提取 | 支持 ASR、关键帧、OCR、evidence 和 `content_pack.json` |
| 学习笔记 | 效率笔记是唯一默认产品路线；质量模式保留为开发调试链路 |
| 播客与音频 | 已能从笔记生成播客稿、`speech.txt`，并通过 Windows 系统语音或可选 MiMo TTS 生成音频 |
| WebUI | 阶段 5.5 已完成三类来源的 fake/offline 浏览器门禁；当前正在把任务控制台、来源、设置、学习库、恢复和回收收敛为无需 CLI 的完整日常工作台，真实 Provider 与人工成品验收仍待最终主入口完成后执行 |
| 恢复与事实 | 任务/批次 JSON 保存事实，SQLite 作为可重建查询投影 |

抖音图文目前只下载为本地素材和 `image_text.json`，尚未进入视频的 ASR、关键帧、证据包和笔记流程。

## 核心流程

```text
本地视频 / 公开 URL / 抖音收藏
                │
                ▼
       字幕 · ASR · 帧 · OCR
                │
                ▼
     evidence + content_pack.json
                │
                ▼
          完整 Markdown 笔记
                │
                ├── 播客稿 / speech.txt
                └── TTS 音频
```

`content_pack.json` 和稳定 `evidence_id` 是生成阶段的共享输入合同。笔记策略、播客稿和 TTS 是不同模块；付费 Provider 阶段必须显式触发或先获得自动工作流授权。

## 当前路线说明

- **效率模式**：`assisted-note` 的 Writer + Reviewer 路线，后续作为默认笔记产品路径。
- **质量模式**：`quality-note`，仍处于开发调试阶段，成功率较低，不属于稳定承诺。
- **严格生成路线**：已退出运行时；历史 V2/V3 产物只保留必要的只读兼容，不应反向影响标准笔记合同。
- **Provider 范围**：稳定 WebUI 维护 MiMo、DeepSeek、MiMo TTS、Windows 系统语音，以及材料提取使用的本地 faster-whisper large-v3 和 PaddleOCR。ASR/OCR 未显式绑定时沿用内置本地默认，显式绑定后会冻结进新任务；保留新增 Provider 的模块接口，但当前不承诺其他 ASR/OCR 服务。
- **TTS 边界**：Windows System.Speech 是默认本地方案，可在设置页选择并保存精确 voice；MiMo TTS 是独立的可选云端连接、密钥引用、预算和失败域。

阶段 0–5.5 已完成共享内核、Provider/TTS、自动收件箱以及三类来源的 fake/offline 浏览器产品门禁。
后续真实单视频纵向验证确认生产基座可行，同时暴露了主 WebUI、应用服务、材料能力绑定和恢复语义仍需
统一。当前阶段先把 WebUI 收敛为普通用户无需 CLI 即可完成添加、设置、授权、观察、预期异常恢复、
任务管理、阅读和播放的完整日常工作台；完成最终主入口的机器门禁后，再进行真实 Provider、
多来源稳定性与人工成品质量验收。本机维护者若配置了 `LOCAL-RECORDS.md`，应以其中指向的现役
实施 Plan 和唯一 State 为准。

## 快速开始

### 环境

- Windows
- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- FFmpeg 与 FFprobe
- 真实 ASR 所需的本地模型缓存

```powershell
git clone <你的 LearnNest 仓库地址>
Set-Location .\LearnNest
uv sync
```

处理一个本地视频：

```powershell
uv run learnnest process `
  --profile evidence `
  --output-root .\learnnest-output `
  '.\lesson.mp4'
```

日常启动本地 WebUI，可直接双击仓库根目录的 `启动语栖.bat`。首次启动会让用户选择学习内容
保存位置，以后直接复用同一位置；配置只保存输出根，不保存密钥。等价命令为：

```powershell
uv run learnnest web launch
```

在“设置 → 本地存储”中可以查看当前运行使用的完整路径，并保存下次正常启动要使用的新位置。
切换位置需要重启语栖才会生效，不会搬迁或删除原位置中的任务。

维护者也可以显式指定输出根：

```powershell
uv run learnnest web serve --output-root .\learnnest-output
```

页面会打开 `http://127.0.0.1:8765`。服务只监听本机回环地址，不提供云端账号、后台服务或
局域网托管；关闭启动进程后，前台自动整理协调器随之停止。

查看当前 CLI：

```powershell
uv run learnnest --help
uv run learnnest doctor
```

`doctor` 不应下载模型或产生付费调用；完整检查需要本机 ASR/OCR 环境以及 `src/learnnest/fixtures/README.md` 说明的本地 fixture。

## 生成阶段与付费边界

基础材料管线不需要付费 LLM。当前代码中的笔记、播客与 TTS 命令分别显式运行；`process`、`queue run` 和本地恢复不得暗中调用付费 Provider。

```powershell
uv run learnnest assisted-note --help
uv run learnnest podcast --help
uv run learnnest tts --help
```

MiMo、DeepSeek、MiMo TTS、Windows TTS、本地 faster-whisper large-v3 和 PaddleOCR 连接都可通过 WebUI 维护，Windows TTS 可选择精确 voice。本地 ASR/OCR 不需要 API Key；未显式绑定时继续使用内置本地能力，显式绑定后与其他职责一样在任务开始时冻结，设置变化不会改写运行中任务。
`.env` 只保留显式旧 CLI 的迁移兼容入口，具体参数以命令帮助和
[`.env.example`](.env.example) 为准。

自动工作流默认关闭，并由用户显式授权。授权冻结重试、全局预算和
note/podcast/TTS/ASR/OCR 五组预算；所有远程调用在构造 Provider 前先写入
running 调用事实。自动重试只能处理明确的临时错误，所有尝试必须可见并计数；
结果为 `unknown` 时停止，不能跨 Provider 自动切换。本地视频、公开 URL 与用户选择的历史
收藏在确定性来源任务成功后进入同一持久化收件箱；首次收藏同步只建立历史基线，新收藏自动
整理还需要单独开启开关并保持有效授权。

## 本地优先与安全

- API Key、Cookie、原始 Provider 请求、模型、媒体和运行产物不进入 Git。
- Provider API Key 使用 Windows 当前用户 DPAPI 加密在对应输出根下；设置 API、任务、日志和页面只保存或显示状态与引用，不回显 Key。
- WebUI 的抖音登录态使用 Windows 当前用户 DPAPI 加密，保存在对应输出根的 `.learnnest/douyin/session.dpapi`。
- 程序不读取日常浏览器配置，不把解密 Cookie 写回 `.env`、任务、日志或 SQLite。
- 请只处理你有权访问、下载和使用的内容，并遵守目标平台规则和适用法律。

不同版本可能使用不同锁目录；切换代码版本前，先停止正在访问同一输出根的旧进程。

## 开发

主要模块边界：

```text
来源适配
  → 确定性材料管线
  → evidence / content_pack
  → 笔记策略
  → 标准笔记
  → 播客稿 / speech.txt
  → TTS
  → WebUI / CLI
```

WebUI 应调用应用服务，不直接拼接底层 CLI 或 Provider。ASR、OCR、LLM 和 TTS 按能力注册；笔记和播客按工作流职责引用 LLM Provider。

常用验证：

```powershell
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run learnnest --help
uv run learnnest doctor
git diff --check
```

维护代码前请阅读 [AGENTS.md](AGENTS.md)；兼容入口见 [AGENT.md](AGENT.md)。

## 许可证

许可证将在首次正式公开发布前确定。
