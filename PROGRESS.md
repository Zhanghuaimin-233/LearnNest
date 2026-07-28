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
