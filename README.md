<p align="center">
  <img src="assets/brand/icon.png" width="144" alt="语栖 LearnNest 图标">
</p>

<h1 align="center">语栖 · LearnNest</h1>

<p align="center">
  把散落在视频、声音与画面里的知识，带回自己的学习之巢。
</p>

<p align="center">
  <strong>收藏夹发现</strong> · <strong>视频下载与转录</strong> · <strong>多模态证据整理</strong> · <strong>可追溯学习输出</strong>
</p>

---

我们收藏过许多“以后一定会看”的视频。

它们可能来自抖音收藏夹、公开视频链接，也可能只是硬盘中一个尚未整理的课程录像。随着收藏越来越多，真正留下来的却往往只有一个链接、一段模糊的印象，或者一句“等有空再看”。

**语栖希望让这些内容真正沉淀下来。**

它会取得视频与字幕，执行 ASR 转录、关键帧抽取和 OCR，将分散的信息整理为结构化证据，再按需生成可以阅读、检索和复查的笔记、播客稿与音频。

> 语栖在生成结论之前，它会先保存字幕、画面、OCR 和来源关系，让每一条重要内容都有迹可循。

## 一条内容如何在语栖中安顿下来

```text
视频任务：抖音收藏夹 / 公开视频链接 / 本地视频
                  │
                  ▼
            发现与获取内容
                  │
                  ▼
        平台字幕或本地 ASR 转录
                  │
                  ▼
          关键帧抽取与 OCR
                  │
                  ▼
      evidence + content_pack.json
                  │
                  ▼
       可追溯 Markdown 学习笔记
                  │
                  ├── 可选：播客稿
                  └── 可选：TTS 音频
```

前半段是一条本地优先、可验证、可重跑的确定性管线。

> 此流程当前只适用于视频任务。抖音收藏夹中的图文会单独下载为本地素材及 `image_text.json`；它尚未进入 ASR、关键帧、OCR、证据包或笔记阶段。

`note`、`podcast` 和 `tts` 分别由显式命令触发。无论是否已经配置 API Key，`process`、`queue run`、`flow run` 和恢复流程都不会暗中触发付费服务。

## 语栖现在可以做什么

| 能力        | 当前用途                                 |
| --------- | ------------------------------------ |
| 抖音收藏夹扫描   | 发现默认视频收藏夹中的新作品，并写入可恢复的待下载队列。         |
| 公开视频导入    | 通过 `yt-dlp` 获取公开视频及可用的平台字幕。          |
| 本地视频处理    | 处理单个视频、目录或任务清单，不依赖第三方平台。             |
| ASR 与 OCR | 从语音和画面中提取字幕、操作信息、界面文字与关键内容。          |
| 证据整理      | 使用稳定的 `evidence_id`、来源类型和文件哈希保存事实关系。 |
| 学习笔记      | 将经过校验的证据渲染为可检索、可回溯的 Markdown 笔记。     |
| 播客与音频     | 基于已经激活的笔记，按需生成口语化播客稿与音频。             |
| 批次与恢复     | 保存任务、批次和重试事实，在失败或中断后继续处理。            |

## 快速开始

### 环境要求

* Windows
* Python 3.12
* [uv](https://docs.astral.sh/uv/)
* FFmpeg 与 FFprobe
* 真实 ASR 所需的本地模型缓存和适配硬件环境

### 安装

```powershell
git clone <你的 LearnNest 仓库地址>
Set-Location .\LearnNest
uv sync
```

### 从一个本地视频开始

```powershell
uv run learnnest process `
  --profile evidence `
  --output-root .\learnnest-output `
  '.\lesson.mp4'
```

语栖会在输出目录中保存：

```text
字幕与时间片段
关键帧与 OCR
evidence.json
content_pack.json
任务状态与处理报告
可阅读的证据追溯视图
```

### 导入一个公开视频

```powershell
uv run learnnest process `
  --profile evidence `
  --output-root .\learnnest-output `
  '<公开视频链接>'
```

公开链接由当前安装的 `yt-dlp` 处理。平台规则、登录要求和 extractor 能力可能随时间变化，因此实际支持情况以本机运行结果为准。

链接无法下载时，也可以先手动取得视频，再作为本地文件交给语栖处理。

### 只预检，不正式处理

```powershell
uv run learnnest process `
  --dry-run `
  --profile evidence `
  --output-root .\learnnest-output `
  '<本地文件或公开视频链接>'
```

`dry-run` 会校验输入、输出路径、所需命令（FFmpeg、FFprobe，以及公开链接所需的 `yt-dlp`），并预览任务身份与重复风险；它不会下载视频、调用模型或写入正式任务结果。运行时 Cookie/API Key 与 ASR/OCR provider 可用性请通过 `readiness` 或 `doctor` 检查。

## 从抖音收藏夹开始

语栖当前已经支持抖音默认视频收藏夹的前台扫描、增量发现、去重和待下载队列。

Cookie 只在运行时读取，不写入任务、日志、数据库或 Git。

```powershell
$env:DOUYIN_COOKIE = Read-Host 'Douyin Cookie' -MaskInput

uv run learnnest readiness `
  --douyin `
  --output-root .\learnnest-output

uv run learnnest schedule add-douyin `
  douyin-favorites `
  '<你的抖音收藏页 URL>' `
  --output-root .\learnnest-output
```

只扫描收藏夹并写入待下载队列：

```powershell
uv run learnnest schedule run `
  douyin-favorites `
  --output-root .\learnnest-output
```

首次使用时，可以只消费最新发现的一条内容：

```powershell
uv run learnnest download pending `
  --schedule-id douyin-favorites `
  --latest-only `
  --output-root .\learnnest-output
```

也可以将“扫描、下载、处理”组合成一次前台操作：

```powershell
uv run learnnest flow run `
  douyin-favorites `
  --latest-only `
  --output-root .\learnnest-output
```

语栖只维护自己的前台计划与任务事实，**不会自动注册或修改 Windows 计划任务**。

## 按需开启 AI 能力

语栖的基础证据管线不需要付费模型。

只有在你显式运行相应命令时，才会调用笔记、播客或 TTS provider。

复制 [`.env.example`](.env.example) 为本地 `.env`，或者只在当前 PowerShell 会话中设置环境变量。`.env` 不应提交到 Git。

| 用途                     | 环境变量                                                                      |
| ---------------------- | ------------------------------------------------------------------------- |
| 抖音收藏夹扫描与下载             | `DOUYIN_COOKIE`                                                           |
| MiMo 笔记、播客与 TTS        | `MIMO_API_KEY`                                                            |
| OpenAI-compatible 笔记服务 | `LEARNNEST_NOTE_API_KEY`、`LEARNNEST_NOTE_BASE_URL`、`LEARNNEST_NOTE_MODEL` |
| 本地 ASR 模型缓存            | `HUGGINGFACE_HUB_CACHE`                                                   |

生成一篇受约束、可校验的学习笔记：

```powershell
$env:MIMO_API_KEY = Read-Host 'MiMo API Key' -MaskInput

uv run learnnest note `
  <task_id> `
  --template concept-explanation `
  --output-root .\learnnest-output
```

继续生成播客稿和音频：

```powershell
uv run learnnest podcast `
  <task_id> `
  --output-root .\learnnest-output

uv run learnnest tts `
  <task_id> `
  --output-root .\learnnest-output
```

笔记、播客稿和音频都由显式命令生成，可分别校验和重跑；但生成顺序有依赖：播客稿需要已激活且校验通过的笔记，TTS 会进一步复验笔记、内容包、播客稿和 `speech.txt`。

## 为什么强调“证据”

普通的视频摘要很容易遇到一个问题：

> 生成的内容看起来合理，却无法确认它究竟来自视频，还是来自模型自己的补充。

语栖会明确区分：

* 字幕证据
* OCR 证据
* 关键帧证据
* AI 补充内容
* 最终渲染产物

重要结论和操作步骤通过稳定的 `evidence_id` 关联原始材料。Markdown 链接、任务状态、上游 SHA 和发布文件也会经过校验。

因此，语栖输出的不只是“答案”，还有答案从哪里来的路径。

## 平台支持

| 来源              | 当前状态                             |
| --------------- | -------------------------------- |
| 抖音默认视频收藏夹       | 已支持扫描、增量发现和待下载队列                 |
| 抖音公开作品链接        | 通过通用 URL 导入处理                    |
| Bilibili 公开视频链接 | 通过 `yt-dlp` 尽力支持                 |
| YouTube 公开视频链接  | 通过 `yt-dlp` 尽力支持                 |
| 其他公开站点          | 取决于本机 `yt-dlp` extractor 与目标站点限制 |
| 本地视频文件与目录       | 完整支持，不依赖平台                       |

目前只有抖音默认视频收藏夹拥有专用扫描适配器。

Bilibili 收藏夹、YouTube 播放列表或频道订阅等专用发现能力尚未实现，但平台发现层与后续处理管线已经解耦。未来新增平台时，只需输出稳定的逻辑来源，不需要重写下载、转录、OCR 和证据链。

请只处理你有权访问、下载和使用的内容，并遵守目标平台规则与适用法律。

## 本地优先与隐私边界

语栖默认将所有任务产物保存在你指定的输出目录中。

```text
<output-root>/
├─ 视频学习素材/       # 任务事实、视频和确定性阶段产物
├─ 视频学习批次/       # 批次、计划与发现/待下载队列
├─ 视频学习笔记/       # 已发布 Markdown 笔记
├─ 视频学习音频/       # 已发布音频
├─ 抖音图文素材/       # 抖音图文下载内容（按需产生）
└─ .learnnest/
   ├─ index.sqlite3    # 可重建 SQLite 查询投影
   └─ locks/           # 跨进程锁
```

任务与批次 JSON 是事实来源，SQLite 只是可删除、可重建的查询投影。

以下内容不会进入 Git：

* API Key
* Cookie
* 原始 provider 请求
* 本地媒体
* 模型缓存
* 运行时任务产物

旧版与新版的锁目录可能不同。切换版本前应先停止旧进程，避免在同一输出根目录中并发运行两个不兼容版本。

## 项目结构

```text
src/learnnest/
├─ adapters/          平台收藏与来源发现
├─ downloader.py      公开视频和平台字幕获取
├─ pipeline.py        主处理管线
├─ stages.py          确定性处理阶段
├─ worker.py          ASR 与 OCR 独立工作进程
├─ models.py          任务、证据与生成产物契约
├─ task_store.py      JSON 事实存储
├─ batch_*.py         批次、队列与恢复
├─ note_*.py          受约束笔记生成与校验
├─ podcast_*.py       播客稿生成与校验
├─ tts_*.py           音频生成与发布对账
├─ rendering.py       确定性 Markdown 渲染
├─ scheduler.py       前台扫描计划
└─ locks.py           跨进程资源锁
```

语栖坚持几个不变的原则：

1. 标题可以修改，稳定任务身份不能依赖标题。
2. 原始证据与 AI 补充必须保持区分。
3. LLM 只生成受约束结构，链接和 Markdown 由程序渲染。
4. 任何阶段都不能暗中触发付费 provider。
5. 下游失败不能破坏已经验证的上游产物。
6. 数据库可以重建，任务事实不能只存在数据库里。
7. 新平台适配器不能绕过统一处理管线。

## 诊断与开发

`doctor` 会在独立子进程中检查 ASR 和 OCR provider，不调用付费服务，也不会主动下载模型。

完整实测需要本地 smoke fixture：

```text
src/learnnest/fixtures/README.md
```

公开仓不会分发测试用音频与图片。fixture 缺失时，命令会明确报告，而不是静默跳过真实验证。

常用开发命令：

```powershell
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run learnnest --help
uv run learnnest doctor
```

维护代码前请阅读 [AGENT.md](AGENT.md)，完整工程合同见 [AGENTS.md](AGENTS.md)。

## 当前进度

当前已经形成的核心路径：

```text
本地视频 / 公开视频 / 抖音收藏夹
              ↓
      下载、字幕、ASR 与 OCR
              ↓
       证据包与内容包
              ↓
        可追溯学习笔记
              ↓
       可选播客稿与音频
```

GeneratedNote 4.0 已具备模板约束、证据校验、快照重渲染和可选审验能力。

目前仍在进行多内容包的人工质量验收。程序层面的 `source_valid` 表示引用结构有效，并不等同于内容已经通过人工质量审核。

后续将继续完善：

* 更多平台的专用发现适配器
* 多内容类型的质量评测
* 长视频与多模态理解
* 更稳定的恢复和自动化体验
* 面向稳定公开发布的安装与迁移流程

## 许可证

许可证将在首次公开发布前确定。

---

<p align="center">
  <strong>让收藏不再只是收藏，让知识在自己的设备上安静栖息。</strong>
</p>
