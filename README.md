<p align="center">
  <img src="assets/brand/icon.png" width="144" alt="语栖 LearnNest 图标">
</p>

<h1 align="center">语栖 · LearnNest</h1>

<p align="center">
  在 Windows 本机把视频整理成可追溯的 Markdown 笔记，并按需生成播客音频。
</p>

LearnNest 是面向个人学习场景的本地优先工具。它把字幕、语音、关键帧和 OCR 整理成带来源关系的材料包，再生成完整笔记；需要时，还能继续生成播客稿和音频。任务事实、凭据和内容默认保存在用户选择的本地目录中。

项目已经跑通“视频 → 材料包 → 完整笔记 → 播客音频”的核心路线，当前仍处于产品可用性收敛阶段。普通用户优先使用本地 WebUI；CLI/API 保留给 Agent、自动化和高级用户。

## 主要能力

- **三类内容入口：** 本地视频、公开 URL，以及经过用户登录授权的抖音收藏。
- **多模态材料提取：** 字幕、faster-whisper ASR、关键帧、PaddleOCR、稳定 `evidence_id` 与 `content_pack.json`。
- **完整学习笔记：** 默认使用 `assisted-note` 的 Writer + Reviewer 效率路线；`quality-note` 仍是开发调试模式。
- **播客与音频：** 从已验证的播客稿和 `speech.txt` 生成音频，支持 Windows System.Speech 和可选 MiMo TTS。
- **本地任务工作台：** 在任务行中查看进度、失败原因和下一步，支持持久暂停、继续、重试、阅读笔记与播放音频。
- **本地模型管理：** 在 WebUI 中显式下载、取消和检查 faster-whisper large-v3 与 PaddleOCR 检测/识别模型；默认使用程序目录旁的 `model`，也可切换到其他可写绝对路径。
- **BYOK Provider 设置：** 提供 17 个 LLM 预设，新增连接必须先获取支持目录并由用户明确选择模型，不会静默使用默认模型或跨供应商回退。
- **可恢复事实：** TaskRecord/BatchManifest JSON 保存任务事实，SQLite 只作为可重建的查询投影。

当前 LLM 预设包括 MiMo、DeepSeek、OpenAI、Kimi、智谱 GLM、阿里云百炼/Qwen、火山方舟/豆包、腾讯混元、MiniMax、LongCat、蚂蚁百灵、xAI、OpenRouter、ModelScope、NVIDIA NIM、Anthropic 和 Gemini。

> [!IMPORTANT]
> 17 个预设的设置流程已通过 fake/offline 机器验证和 WebUI 功能复核；除 MiMo、DeepSeek 外，新增的 15 个 Provider 尚未逐项完成真实目录、真实连接和成品调用验收。配置成功不代表对应账号、模型或最终产出已经验证。

## 当前边界

- 目前只正式面向 Windows 与 Python 3.12；WebUI 仅监听 `127.0.0.1`，不是云服务或局域网服务。
- 抖音图文目前只保存本地素材和 `image_text.json`，尚未接入视频的 ASR、证据包与笔记流程。
- 本地模型只覆盖现有 ASR/OCR adapter 所需资产，不包含本地 LLM、Ollama 或新的 TTS 引擎。
- 页面加载、启动、测试、`doctor` 与任务执行不会隐式下载模型，也不会隐式发起付费 Provider 调用。
- 请只处理你有权访问、下载和使用的内容，并遵守来源平台规则与适用法律。

## 工作流程

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

`content_pack.json` 和稳定 `evidence_id` 是生成阶段的共享输入合同。笔记、播客稿和 TTS 是可独立配置、失败和重跑的阶段。

## 快速开始

### 1. 准备环境

- Windows
- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- FFmpeg 与 FFprobe

```powershell
git clone https://github.com/Zhanghuaimin-233/LearnNest.git
Set-Location .\LearnNest
uv sync
```

### 2. 启动 WebUI

日常使用可直接双击仓库根目录的 `启动语栖.bat`，或运行：

```powershell
uv run learnnest web launch
```

首次启动会让你选择学习内容的保存位置；以后会复用该位置。页面打开在 `http://127.0.0.1:8765`。关闭启动进程后，本地 Web 服务和前台自动整理协调器随之停止。

进入“设置”后可以：

1. 在“本地存储”查看当前输出目录，或保存下次启动使用的新目录；
2. 在“本地模型”确认模型目录，显式下载 ASR/OCR 模型；
3. 在“API 连接”新增云端连接、获取模型目录并选择具体模型；
4. 在“职责配置”把连接绑定到 Writer、Reviewer、Podcast 或 TTS；
5. 在“调用与限额”检查预算，最后显式开启付费整理许可。

切换输出目录需要重启才会生效，不会搬迁或删除旧目录。切换模型目录即时生效，但下载进行中会被拒绝，旧模型也不会自动搬迁或删除。

### 3. 使用 CLI

只运行确定性材料管线：

```powershell
uv run learnnest process `
  --profile evidence `
  --output-root .\learnnest-output `
  '.\lesson.mp4'
```

查看完整命令：

```powershell
uv run learnnest --help
uv run learnnest doctor
uv run learnnest assisted-note --help
uv run learnnest podcast --help
uv run learnnest tts --help
```

`doctor` 只检查本机 ASR/OCR worker，不应下载模型或产生付费调用。完整检查需要本地模型以及 [`src/learnnest/fixtures/README.md`](src/learnnest/fixtures/README.md) 中说明的 fixture。

## Provider、费用与隐私

- API Key 使用 Windows CurrentUser DPAPI 加密，按输出根保存；页面、设置 API、任务、日志和 SQLite 不保存或回显明文。
- 云端“检查连接”是一次显式诊断：必须确认可能付费，每次只发送一个最小真实请求，不自动重试，也不保存 Provider 返回正文。
- 付费整理许可默认关闭。保存设置、授予许可和开始整理是三个独立动作，LearnNest 不会替用户完成确认。
- 重试仅覆盖明确的临时错误；`unknown`、永久错误和配置错误立即停止。所有真实尝试都会持久化并计数。
- Provider、模型、重试次数、预算或自动来源范围发生实质变化时，旧授权会失效。
- API Key、Cookie、原始 Provider 请求、模型、媒体和运行产物不得提交到 Git。

`.env` 只保留旧 CLI 的显式迁移兼容入口，参数见 [`.env.example`](.env.example)。

## 开发与验证

维护代码前请阅读 [AGENTS.md](AGENTS.md)；兼容入口见 [AGENT.md](AGENT.md)。主要模块按以下边界协作：

```text
来源适配
  → 确定性材料管线
  → evidence / content_pack
  → 笔记策略
  → 标准笔记
  → 播客稿 / speech.txt
  → TTS
  → 应用服务
  → WebUI / CLI
```

常用验证：

```powershell
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run learnnest --help
uv run learnnest doctor
git diff --check
```

## 许可证

本项目采用 [MIT License](LICENSE)。
