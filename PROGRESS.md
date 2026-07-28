# WebUI MVP progress

## 2026-07-28 — implementation started

- Confirmed `process_source` / `process_video` are the deterministic processing boundary and `plan_recovery` / `rerun_task` are the recover boundary.
- Confirmed the worktree has unrelated, uncommitted automation changes. They will be read only; no existing automation file will be edited.
- Planned scope: loopback-only FastAPI + Uvicorn service, static Chinese task workspace, in-memory job status, and API tests with fake pipeline functions.
- Added the loopback service, `learnnest web serve`, static workspace, and WebUI-focused test coverage.

## 2026-07-28 — verification complete

- `uv run pytest -q tests/test_web_app.py`: 8 passed. The test run emits one upstream FastAPI TestClient / Starlette deprecation warning only.
- Loopback smoke: started the actual Uvicorn server on `127.0.0.1:18765`; `/healthz` returned the loopback payload and `/` returned the Chinese workspace page, then the server stopped cleanly.
- `uv run pytest -q`: 953 passed in 101.10s, with the same one upstream test-client warning.
- `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run learnnest web serve --help`, `uv run learnnest --help`, `uv run learnnest doctor`, and `git diff --check`: passed.
- No source, media, ASR, OCR, LLM, TTS, paid provider, or Windows Task Scheduler operation was invoked during verification.

## 2026-07-28 — human-first vertical slice

- Task 0 passed: `tests/test_web_app.py` 8 passed; full `pytest` 953 passed, skipped 0; Ruff check and format check passed (only the existing Starlette TestClient deprecation warning).
- Current dirty ownership: prior work owns `README.md`/`pyproject.toml`/`uv.lock`, assisted-note/CLI/lock/podcast changes, all automation files, the existing WebUI/static files and their tests; this task may only merge the explicitly allowed human-first files.
- Goal: a free local vertical slice—add content, default to existing `profile="note"`, automatically observe task JSON, and open a safe published note without exposing pipeline terms.
- Order: pure application layer → human API/snapshot and safe rendering → learning inbox page → ASGI and full gates.
- Maximum risk: a deterministic `note` profile may not yield a published note for every fixture; tests must prove the real publish contract rather than fabricate a library item.

## 2026-07-28 — task 1 and task 2 complete

- Task 1 complete: added pure `LearningWorkspace` that only projects task JSON; it maps the two human outputs to existing `note`/`evidence` profiles, isolates damaged records, continues only deterministic work, and refuses paid recovery with a settings message.
- Task 2 complete: added the `/api/learning/*` surface with revisioned snapshot reads, safe Markdown rendering (`html=False`), and image access limited to the current note's declared task-local references.
- Focused verification after implementation: 19 passed across `tests/test_learning_workspace.py` and `tests/test_web_app.py`; no heavy media/provider boundary was invoked.

## 2026-07-28 — task 3 complete

- Replaced the default static page with the four human sections and 2 s / hidden 5 s bounded snapshot polling. Visual inspection corrected the mobile horizontal crop; screenshots are in the Codex visualizations folder.
- Known environment blocker: an existing `learnnest.exe web serve` holds the virtual-environment launcher. Ordinary `uv run` now cannot sync after the allowed dependency change; see `BLOCKED.md`. This is not a test failure or a reason to loosen any assertion.

## 2026-07-28 — task 4 complete except ordinary sync retry

- Final gate in the already installed locked environment: `uv run --no-sync pytest -q tests/test_learning_workspace.py tests/test_web_app.py` = 19 passed; full suite = 964 passed, skipped 0; Ruff check/format, doctor, CLI help and both repositories' `diff --check` passed.
- Security/UI checks: default-page forbidden-text scan = 0; the new page references only `/api/learning/*`; ASGI tests cover published-note opening, external atomic task update in under 3 seconds, HTML escaping, escaped/unreferenced image denial, paid-continue denial and damaged-record isolation.
- Visual review: desktop and phone screenshots (`learnnest-human-inbox-1280.png` and `learnnest-human-inbox-mobile.png`) were captured outside the repository and inspected; the phone layout was corrected once for horizontal clipping.
- Still pending only the ordinary synchronized `uv run` command retry after the pre-existing server releases `learnnest.exe`; no provider, media processing, download, ASR/OCR, TTS, paid call or Windows Task Scheduler action was invoked.

## 2026-07-28 — ordinary-sync retry remains blocked

- Final read-only retry confirmed the existing `learnnest.exe web serve --output-root .\learnnest-output` process still owned the repository-local launcher required by `uv` environment synchronization. No normal `uv run` gate was reissued while that external state was unchanged, and the process was not stopped.

## 2026-07-28 — third blocked audit

- A third consecutive read-only check found the same PID 29448 command still running and holding the same launcher. All implementation, focused/full `--no-sync` gates, static scan and visual review remain complete; only the task-required ordinary synchronized `uv run` retry awaits that external process exiting. No safe in-scope action can release it.

## 2026-07-28 — completion gate passed

- The existing server exited without intervention. Ordinary synchronized `uv run` now passed: focused human layer 19 passed; full suite 964 passed, skipped 0; Ruff check/format, doctor, CLI help and both repositories' `diff --check` passed. The only warning is the existing Starlette TestClient deprecation; `uv` additionally reported a non-failing hardlink-to-copy fallback during environment sync.
- `BLOCKED.md` now records “无”. The free local slice remains the delivered boundary: provider setup, paid generation, automation, Windows scheduling and advanced diagnostics are intentionally not connected.

## 2026-07-28 — public URL acceptance fix

- Manager acceptance found that both Web submission APIs accepted explicit loopback and private-network URLs even though the page promises a public link. Root cause: the Web layer reused the general CLI source parser, whose contract validates HTTP(S) syntax rather than public-network scope.
- Added one shared Web-only public-host policy. The human and legacy Web submission APIs now reject loopback, private, link-local, special-use and local hostname forms before the pipeline is called; CLI source behavior remains unchanged. The general URL normalizer now also preserves IPv6 brackets.
- Independent ASGI recheck: six explicit local/private inputs returned 400 without a pipeline call; `https://example.com/video` returned 201 and used profile `note`.
- Fresh verification: focused source/human/Web tests 41 passed; full suite 976 passed, skipped 0; Ruff check/format, doctor, Web CLI help and `git diff --check` passed. The only warning remains the existing Starlette TestClient deprecation.

## 2026-07-28 — 统一重试合同开始

- 目标：本地下载/确定性阶段与 Writer、Reviewer、Podcast、TTS 统一为每阶段四次机会，并以 UTC 日跨任务 provider 总调用 80 次止损。
- 顺序：先核对真实事实与基线，再做阶段账本/恢复、付费预算/unknown、CLI 可观察性、文档和全量门禁。
- 当前基线：`pytest 976 passed, 0 skipped`；Ruff check、configure help、主仓 diff check 通过；format check 发现既有 `cli.py` 格式差异，已记入 `BLOCKED.md`。
- 最大风险：未知付费结果绝不能被下一次调用覆盖；Records 既有 dirty 状态只允许改任务白名单内的 active `STATE.md`。

## 2026-07-28 — 合同实现检查点

- 任务 1：TaskAttempt 持久化执行过的阶段，DiscoveryRecord 持久化下载 `attempt_count`；本地自动恢复复用已有 TaskRecord，四次机会封顶，已加入真实失败序列回归。
- 任务 2：policy 迁移到 `retries_per_stage=3` 与共享 `provider_calls_per_day=80`；policy SHA 排除 enabled/authorized_at，running/unknown/本地恢复/预算阻断已实现。
- 任务 3：tick 先下载/恢复本地 retryable 再构造付费 provider；CLI status 显示 UTC used/limit/remaining、付费阶段 attempt/max、unknown/预算原因；README 与 active STATE 已同步。
- 当前定向合同回归：9 passed；未调用真实下载、ASR/OCR、LLM、Podcast、TTS 或 Windows Task Scheduler。
- 完整回归：`uv run pytest -q` = `986 passed, 0 skipped`；仅有既有 Starlette TestClient 弃用警告。Ruff 最终门禁与 CLI/doctor/两仓 diff check 待最后一轮复核。

## 2026-07-28 — 最终门禁与交接
- `uv run pytest -q`：`986 passed, 0 skipped`，仅有既有 Starlette TestClient 弃用警告；统一合同定向回归 `16 passed`，相关范围定向回归 `92 passed`。
- `uv run ruff check src tests`：`All checks passed!`；`uv run ruff format --check src tests`：`164 files already formatted`。
- `uv run learnnest automation configure --help`、`automation status --help`、`learnnest --help`：均 exit 0；配置帮助显示每阶段 `3` 次重试/`4` 次机会与 UTC 日共享 provider `80` 次；`uv run learnnest doctor`：faster-whisper、paddleocr 均 `OK`。
- 主仓与 `LearnNest-Records` 的 `git diff --check` 均 exit 0，仅有 Windows LF→CRLF 工作副本警告；白名单外没有本轮修改，未提交、未推送、未创建 PR。
- 未执行真实下载、ASR/OCR、LLM/Writer/Reviewer、Podcast、TTS 或 Windows Task Scheduler；`BLOCKED.md` 保留任务 0 的已解决格式差异证据，当前持续阻塞为“无”。
- 最终白名单审计：`WHITELIST OUTSIDE: 0`；Records 本轮仅改 active `STATE.md`，其他既有脏改动未触碰。

## 2026-07-28 — 验收缺口修补

- 独立验收发现三个根因：本地 failure queue 的旧 SQL 仍按整任务累计 retryable 失败；automation policy 的重试值没有传入下载/确定性恢复；provider 日总量只扫描当前 policy SHA。
- 修补后，failure queue 以 TaskRecord 的 `executed_stages` 按失败阶段裁决并按阶段计算退避；`automation tick` 将 policy 的 `1–4` 次机会同时传入 discovery 与本地 recover；普通手动入口继续使用默认四次。
- provider 用量改为扫描所有 policy 的任务事实；新付费调用持久化唯一 `call_id`，旧 policy 迁移副本用稳定 legacy key 去重，因此修改 policy 不会重置 UTC 当日总量。
- 新增回归覆盖：三个 transcript 失败不消耗 OCR 机会、`retries_per_stage=0` 同时关闭下载与本地自动重试、80 次用量跨 policy 变化仍阻止第 81 次调用、旧迁移副本不重复计数。
- 修补专项回归：`55 passed`；完整回归：`991 passed`，仅保留既有 Starlette TestClient 弃用警告。此修补经用户明确授权，因根因路径额外修改 `index.py`、`sql/schema_v1.sql`、`tests/test_execution.py` 与 `tests/test_automation_policy.py`。
