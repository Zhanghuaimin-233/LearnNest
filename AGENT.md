# AGENT.md — LearnNest Agent 入口

这是兼容部分 Agent 工具的简短入口。完整工程合同在 [AGENTS.md](AGENTS.md)，用户使用说明和当前能力见 [README.md](README.md)；不要在本文件复制另一套产品路线。

LearnNest 是 Windows 上本地优先的个人学习产品：把视频整理成完整 Markdown 笔记，并按需生成播客音频。普通用户入口以 WebUI 为主，CLI/API 用于维护、自动化和高级操作。

开始修改前：

1. 使用 Python 3.12 和 `uv run`。
2. 从当前代码、测试和命令确认事实，不从旧计划推断。
3. 保留 `evidence`、`content_pack.json`、任务事实和发布安全等共享合同。
4. 不扩展已废弃的 V4；质量模式仍是实验路线，效率模式是后续默认路线。
5. 笔记、播客稿和 TTS 分模块处理，不把 TTS 绑定到 V4 私有产物。
6. 不提交 `.env`、密钥、Cookie、媒体、模型、原始 Provider 请求或运行产物。

计划、任务 State、报告、验收与 Skill 产物不放在公开仓。当前工作站如有 `LOCAL-RECORDS.md`，按其规则写入私有资料仓；公开仓本身不能依赖该资料仓才能构建和测试。

验证命令与不可破坏合同以 [AGENTS.md](AGENTS.md) 为准。
