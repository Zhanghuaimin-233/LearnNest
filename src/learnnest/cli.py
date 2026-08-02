"""Command-line entry point for learnnest."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import SecretStr

from learnnest import providers
from learnnest.automation_models import AutomationBudget, AutomationPolicy
from learnnest.automation_store import (
    authorize as authorize_automation,
    disable as disable_automation,
    list_incomplete_task_ids,
    load_status as load_automation_status,
    list_task_states,
    provider_call_usage,
    save_policy as save_automation_policy,
)
from learnnest.automation_runner import AutomationProviders, run_automation_tasks
from learnnest.automation_scheduler import (
    install as install_automation_scheduler,
    status as automation_scheduler_status,
    uninstall as uninstall_automation_scheduler,
)
from learnnest.assisted_note_generation import (
    create_assisted_plan,
    generate_assisted_plan,
    load_assisted_plan,
    load_assisted_state,
    recover_assisted_plan,
    review_assisted_plan,
)
from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.artifact_layout import migrate_artifact_layout, plan_artifact_layout
from learnnest.adapters.douyin import DouyinFavoritesAdapter
from learnnest.adapters.douyin_http import DouyinHttpTransport
from learnnest.adapters.folder import FolderAdapter
from learnnest.batch import resume_batch, run_batch
from learnnest.batch_store import load_batch
from learnnest.download_queue import run_pending_downloads
from learnnest.douyin_cookie_store import DouyinCookieStore
from learnnest.execution import plan_recovery
from learnnest.index import (
    IndexRebuildError,
    index_status,
    query_failure_queue,
    query_history,
    query_history_facts,
    rebuild_index,
)
from learnnest.locks import LockUnavailable, schedule_lock, task_lock
from learnnest.note_providers import (
    DEFAULT_NOTE_SAFE_INPUT_TOKENS,
    MimoQualityNoteProvider,
    OpenAICompatibleAssistedNoteProvider,
    OpenAICompatibleChatConfig,
    OpenAICompatibleQualityNoteProvider,
    probe_openai_compatible_writer,
)
from learnnest.provider_profiles import (
    PRESETS,
    ProviderConnection,
    check_capability,
    connect as connect_provider,
    get_connection,
    load_settings as load_provider_settings,
    profile_for_connection,
    set_authorization,
)
from learnnest.quality_execution_models import WriterCapabilitySnapshot
from learnnest.quality_note_generation import (
    create_quality_plan,
    generate_quality_note_plan,
    load_quality_plan,
    load_quality_state,
    organize_quality_plan,
    recover_quality_plan,
    review_quality_note_plan,
)
from learnnest.reader_templates import resolve_reader_template
from learnnest.pipeline import PipelineError, process_source, process_video, rerun_task
from learnnest.podcast_generation import (
    build_and_activate_external_podcast,
    generate_and_activate_podcast,
)
from learnnest.podcast_providers import MimoPodcastProvider
from learnnest.preflight import preflight_runtime, preflight_sources
from learnnest.queue_runner import run_failure_queue
from learnnest.runtime_config import load_runtime_environment
from learnnest.scheduler import ResourceScheduler
from learnnest.schedule_store import (
    list_schedules,
    load_schedule,
    schedule_path,
    write_schedule_atomic,
)
from learnnest.schedules import (
    create_interval_douyin_schedule,
    create_interval_folder_schedule,
    ensure_manual_folder_schedule,
    run_schedule_once,
    tick_schedules,
)
from learnnest.sources import SourceParseError, collect_sources
from learnnest.stages import STAGES
from learnnest.task_store import load_task
from learnnest.tts_generation import (
    DEFAULT_TTS_STYLE,
    generate_and_activate_tts,
)
from learnnest.tts_providers import MimoTtsProvider
from learnnest.web_app import serve_web_app
from learnnest.validation import validate_task

app = typer.Typer(no_args_is_help=True)
index_app = typer.Typer(no_args_is_help=True)
queue_app = typer.Typer(no_args_is_help=True)
download_app = typer.Typer(no_args_is_help=True)
batch_app = typer.Typer(no_args_is_help=True)
scan_app = typer.Typer(no_args_is_help=True)
schedule_app = typer.Typer(no_args_is_help=True)
flow_app = typer.Typer(no_args_is_help=True)
layout_app = typer.Typer(no_args_is_help=True)
quality_note_app = typer.Typer(no_args_is_help=True)
assisted_note_app = typer.Typer(no_args_is_help=True)
provider_app = typer.Typer(no_args_is_help=True)
automation_app = typer.Typer(no_args_is_help=True)
web_app = typer.Typer(no_args_is_help=True)
app.add_typer(
    index_app, name="index", help="Inspect or rebuild the Vault SQLite read model."
)
app.add_typer(
    assisted_note_app,
    name="assisted-note",
    help="Run the isolated Markdown Writer and Reviewer workflow.",
)
app.add_typer(
    provider_app,
    name="provider",
    help="Manage local BYOK connections and Writer capability profiles.",
)
app.add_typer(
    automation_app,
    name="automation",
    help="Configure and inspect explicitly authorized local paid delivery.",
)
app.add_typer(web_app, name="web", help="Run the local task workspace in a browser.")
app.add_typer(
    queue_app, name="queue", help="Inspect the derived retryable failure queue."
)
app.add_typer(
    download_app, name="download", help="Consume persisted discovered download links."
)
app.add_typer(batch_app, name="batch", help="Resume persisted batch execution ledgers.")
app.add_typer(scan_app, name="scan", help="Run one foreground incremental scan.")
app.add_typer(
    schedule_app,
    name="schedule",
    help="Manage project-internal foreground schedules.",
)
app.add_typer(
    flow_app,
    name="flow",
    help="Run explicit monitor-to-delivery foreground workflows.",
)
app.add_typer(
    layout_app,
    name="layout",
    help="Preview or explicitly migrate artifact roots without copying files.",
)
app.add_typer(
    quality_note_app,
    name="quality-note",
    help="Run the explicit quality-first Organizer/Writer/Reviewer workflow.",
)


class ProfileOption(StrEnum):
    """The supported v0.1 pipeline profiles exposed by the CLI."""

    EVIDENCE = "evidence"
    NOTE = "note"
    FULL = "full"


class StageOption(StrEnum):
    """The stages from which a persisted task may be resumed."""

    SOURCE = "source"
    TRANSCRIPT = "transcript"
    FRAMES = "frames"
    OCR = "ocr"
    EVIDENCE = "evidence"
    CONTENT_PACK = "content_pack"
    NOTE = "note"
    PUBLISH = "publish"


class QualityReviewModeOption(StrEnum):
    """Quality-first review policy."""

    NONE = "none"
    REPORT = "report"
    GATE = "gate"


@app.callback()
def main() -> None:
    """Convert local learning videos into traceable evidence packs."""


@provider_app.command("connect")
def provider_connect(
    preset: Annotated[
        str,
        typer.Argument(
            help=(
                "mimo, openai, anthropic, gemini, deepseek, coding-plan, "
                "or openai-compatible"
            )
        ),
    ],
    name: Annotated[
        str, typer.Option("--name", help="Stable local connection name.")
    ] = "default",
    endpoint: Annotated[
        str | None, typer.Option("--endpoint", help="Required for openai-compatible.")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Optional advanced model override.")
    ] = None,
    secret_env: Annotated[
        str | None,
        typer.Option(
            "--secret-env", help="Secret environment variable for openai-compatible."
        ),
    ] = None,
    provider_name: Annotated[
        str | None,
        typer.Option(
            "--provider-name",
            help="Optional stable name for an OpenAI-compatible connection.",
        ),
    ] = None,
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Save a secret-free connection; preset connections prompt only for their key."""
    root = _output_root(output_root)
    if preset != "openai-compatible" and preset not in PRESETS:
        typer.echo("ERROR: unknown provider preset", err=True)
        raise typer.Exit(code=1)
    if preset == "openai-compatible" and (not endpoint or not model or not secret_env):
        typer.echo(
            "ERROR: openai-compatible requires --endpoint, --model, and --secret-env",
            err=True,
        )
        raise typer.Exit(code=1)
    key = typer.prompt("API key", hide_input=True)
    if not key.strip():
        typer.echo("ERROR: API key is required", err=True)
        raise typer.Exit(code=1)
    try:
        connection = connect_provider(
            root,
            name=name,
            preset=preset,
            endpoint=endpoint,
            model=model,
            secret_env=secret_env,
            provider_name=provider_name,
        )
        _write_local_env_value(Path.cwd() / ".env", connection.secret_env, key)
        if load_provider_settings(root).authorization == "automatic":
            if connection.api_family != "openai_chat":
                raise ValueError(
                    "native provider capability adapters are not available in this build"
                )
            config = OpenAICompatibleChatConfig(
                provider_name=connection.provider,
                model=connection.model,
                base_url=connection.endpoint,
                api_key=SecretStr(key),
            )
            profile = check_capability(
                root,
                connection,
                lambda strategy: probe_openai_compatible_writer(config, strategy),
            )
            typer.echo(
                f"Capability profile: {profile.profile_id} / {profile.status} / {len(profile.attempts)} calls"
            )
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"Connected {connection.name}: {connection.provider} / {connection.model}"
    )


@provider_app.command("status")
def provider_status(
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Show local connections and profile state without provider calls."""
    root = _output_root(output_root)
    try:
        settings = load_provider_settings(root)
        typer.echo(f"Authorization: {settings.authorization}")
        for name, connection in sorted(settings.connections.items()):
            profile = profile_for_connection(root, connection)
            state = profile.status if profile is not None else "unchecked"
            marker = " (default)" if settings.default_connection == name else ""
            typer.echo(
                f"{name}{marker}: {connection.provider} / {connection.model} / {state}"
            )
    except ValueError as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error


@provider_app.command("authorize")
def provider_authorize(
    mode: Annotated[str, typer.Argument(help="automatic or local-only")],
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Set whether connection changes may run the separately counted check."""
    value = "local_only" if mode == "local-only" else mode
    if value not in {"local_only", "automatic"}:
        typer.echo("ERROR: authorization must be automatic or local-only", err=True)
        raise typer.Exit(code=1)
    settings = set_authorization(_output_root(output_root), value)  # type: ignore[arg-type]
    typer.echo(f"Authorization: {settings.authorization}")


@provider_app.command("check")
def provider_check(
    connection_name: Annotated[
        str | None,
        typer.Option(
            "--connection", help="Connection name; defaults to the configured default."
        ),
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh", help="Run again even when a verified profile exists."
        ),
    ] = False,
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Run at most four separately counted synthetic Writer contract calls."""
    root = _output_root(output_root)
    try:
        connection = get_connection(root, connection_name)
        current = profile_for_connection(root, connection)
        if current is not None and current.status == "verified" and not refresh:
            typer.echo(f"Capability profile: {current.profile_id} ({current.strategy})")
            return
        if connection.api_family != "openai_chat":
            raise ValueError(
                "native provider capability adapters are not available in this build"
            )
        environment = _runtime_environment()
        secret = environment.get(connection.secret_env, "").strip()
        if not secret:
            raise ValueError(f"{connection.secret_env} is missing")
        config = OpenAICompatibleChatConfig(
            provider_name=connection.provider,
            model=connection.model,
            base_url=connection.endpoint,
            api_key=SecretStr(secret),
        )
        profile = check_capability(
            root,
            connection,
            lambda strategy: probe_openai_compatible_writer(config, strategy),
        )
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"Capability profile: {profile.profile_id} / {profile.status} / {profile.strategy or 'none'} / {len(profile.attempts)} calls"
    )


@app.command()
def doctor() -> None:
    """Verify ASR and OCR in separate provider worker processes."""
    try:
        checked = _verify_provider_workers(_runtime_environment())
    except (KeyError, RuntimeError, json.JSONDecodeError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    for provider in checked:
        typer.echo(f"OK: {provider}")


@app.command()
def readiness(
    douyin: Annotated[
        bool,
        typer.Option("--douyin", help="Require the Douyin monitor prerequisites."),
    ] = False,
    note: Annotated[
        bool,
        typer.Option("--note", help="Require an explicit note provider configuration."),
    ] = False,
    podcast: Annotated[
        bool,
        typer.Option("--podcast", help="Require the MiMo podcast configuration."),
    ] = False,
    tts: Annotated[
        bool,
        typer.Option("--tts", help="Require the MiMo TTS configuration."),
    ] = False,
    verify_providers: Annotated[
        bool,
        typer.Option(
            "--verify-providers/--skip-providers",
            help="Run isolated ASR/OCR checks after static checks pass.",
        ),
    ] = True,
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", help="Vault root."),
    ] = None,
) -> None:
    """Preflight an explicit workflow before discovery, downloads, or paid calls."""
    runtime_environ = _runtime_environment()
    root = _output_root(output_root)
    try:
        douyin_cookie_available = (
            _load_douyin_cookie(root) is not None if douyin else None
        )
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    result = preflight_runtime(
        root,
        require_douyin=douyin,
        require_note=note,
        require_podcast=podcast,
        require_tts=tts,
        runtime_environ=runtime_environ,
        douyin_cookie_available=douyin_cookie_available,
    )
    for issue in result.issues:
        typer.echo(
            f"{issue.severity.upper()}: {issue.code}: {issue.message}",
            err=issue.severity == "error",
        )
    if not result.ok:
        raise typer.Exit(code=1)
    if verify_providers:
        try:
            checked = _verify_provider_workers(runtime_environ)
        except (KeyError, RuntimeError, json.JSONDecodeError) as error:
            typer.echo(f"ERROR: provider_check_failed: {error}", err=True)
            raise typer.Exit(code=1) from error
        for provider in checked:
            typer.echo(f"OK: {provider}")
    typer.echo("Readiness: OK")


@flow_app.command("run")
def flow_run(
    schedule_id: Annotated[str, typer.Argument(help="Stable Douyin monitor ID.")],
    max_items: Annotated[
        int,
        typer.Option("--max-items", min=1, help="Maximum discovered links to consume."),
    ] = 10,
    latest_only: Annotated[
        bool,
        typer.Option(
            "--latest-only",
            help="One-shot test selector; never changes monitor cursor semantics.",
        ),
    ] = False,
    with_podcast: Annotated[
        bool,
        typer.Option("--with-podcast", help="Generate a podcast after the note."),
    ] = False,
    with_tts: Annotated[
        bool,
        typer.Option("--with-tts", help="Generate audio after the podcast."),
    ] = False,
    confirm_paid: Annotated[
        bool,
        typer.Option(
            "--confirm-paid", help="Required before any requested paid stage."
        ),
    ] = False,
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", help="Vault root."),
    ] = None,
) -> None:
    """Run one monitor, consume its links, then optionally run paid stages once."""
    if with_tts and not with_podcast:
        typer.echo("ERROR: --with-tts requires --with-podcast", err=True)
        raise typer.Exit(code=1)
    paid_requested = with_podcast or with_tts
    if paid_requested and not confirm_paid:
        typer.echo("ERROR: paid stages require --confirm-paid", err=True)
        raise typer.Exit(code=1)
    root = _output_root(output_root)
    runtime_environ = _runtime_environment()
    try:
        douyin_cookie = _load_douyin_cookie(root)
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    readiness_result = preflight_runtime(
        root,
        require_douyin=True,
        require_note=False,
        require_podcast=with_podcast,
        require_tts=with_tts,
        runtime_environ=runtime_environ,
        douyin_cookie_available=douyin_cookie is not None,
    )
    for issue in readiness_result.issues:
        typer.echo(
            f"{issue.severity.upper()}: {issue.code}: {issue.message}",
            err=issue.severity == "error",
        )
    if not readiness_result.ok:
        raise typer.Exit(code=1)
    secret: SecretStr | None = None
    try:
        _verify_provider_workers(runtime_environ)
        schedule = load_schedule(schedule_path(root, schedule_id))
        outcome = run_schedule_once(
            root,
            schedule_id,
            adapter_factory=_schedule_adapter,
            runtime_credentials=douyin_cookie,
        )
        if outcome.status not in {"completed", "partial"}:
            raise RuntimeError(
                f"monitor did not complete: {outcome.status}"
                + (f" ({outcome.error})" if outcome.error else "")
            )
        downloads = run_pending_downloads(
            root,
            max_items=1 if latest_only else max_items,
            profile=schedule.profile,
            schedule_id=schedule_id,
            selection="latest_observed" if latest_only else "oldest",
            douyin_cookie=douyin_cookie,
        )
        if downloads.failed_count:
            raise RuntimeError("download consumer reported failed links")
        if paid_requested and not downloads.task_ids:
            raise RuntimeError(
                "the selected links produced no video tasks; use individual commands "
                "for existing tasks or choose video links"
            )
        for task_id in downloads.task_ids:
            task_dir = _find_task_dir(task_id, root)
            if with_podcast:
                with _task_execution_lock(root, task_dir):
                    secret = _mimo_api_key(runtime_environ)
                    with _resource_execution(root, "network", "llm"):
                        generate_and_activate_podcast(
                            task_dir, MimoPodcastProvider(secret), root
                        )
            if with_tts:
                with _task_execution_lock(root, task_dir):
                    secret = _mimo_api_key(runtime_environ)
                    with _resource_execution(root, "network", "tts", "ffmpeg"):
                        generate_and_activate_tts(
                            task_dir, MimoTtsProvider(secret), root
                        )
    except LockUnavailable as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, PipelineError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {_safe_error(error, secret)}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"Flow completed: schedule={schedule_id} "
        f"claimed={downloads.claimed_count} tasks={len(downloads.task_ids)}"
    )


@automation_app.command("configure")
def automation_configure(
    schedule_id: Annotated[
        str, typer.Argument(help="Default Douyin monitor schedule ID.")
    ],
    writer_connection: Annotated[
        str | None,
        typer.Option(
            "--writer-connection",
            help="Writer connection; defaults to configured default.",
        ),
    ] = None,
    reviewer_connection: Annotated[
        str | None,
        typer.Option(
            "--reviewer-connection", help="Reviewer connection; defaults to Writer."
        ),
    ] = None,
    max_items: Annotated[
        int, typer.Option("--max-items", min=1, max=20, help="Maximum videos per tick.")
    ] = 1,
    retries_per_stage: Annotated[
        int,
        typer.Option(
            "--retries-per-stage",
            "--paid-retry-limit",
            min=0,
            max=3,
            help="Automatic retries per stage; 3 retries means 4 total opportunities.",
        ),
    ] = 3,
    provider_calls_per_day: Annotated[
        int,
        typer.Option(
            "--provider-calls-per-day",
            min=0,
            max=800,
            help="Shared UTC-day provider call count (not an amount of money).",
        ),
    ] = 80,
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Persist a disabled, secret-free automatic-delivery policy without calls."""
    root = _output_root(output_root)
    try:
        schedule = load_schedule(schedule_path(root, schedule_id))
        if schedule.source.kind != "douyin":
            raise ValueError("automation currently supports only a Douyin schedule")
        writer = _assisted_connection_snapshot(get_connection(root, writer_connection))
        reviewer = _assisted_connection_snapshot(
            get_connection(root, reviewer_connection or writer_connection)
        )
        policy = AutomationPolicy(
            schedule_id=schedule_id,
            writer=writer,
            reviewer=reviewer,
            max_items_per_tick=max_items,
            retries_per_stage=retries_per_stage,
            budget=AutomationBudget(provider_calls_per_day=provider_calls_per_day),
        )
        status = save_automation_policy(root, policy)
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        "Automation configured: disabled "
        f"schedule={status.policy.schedule_id} "
        f"retries_per_stage={status.policy.retries_per_stage} "
        f"opportunities_per_stage={status.policy.retries_per_stage + 1} "
        f"provider_calls_per_day={status.policy.budget.provider_calls_per_day} "
        f"policy={status.policy_sha256[:12]}"
    )


@automation_app.command("authorize")
def automation_authorize(
    confirm_paid: Annotated[
        bool,
        typer.Option(
            "--confirm-paid",
            help="Acknowledge that automatic Writer, Reviewer, podcast, and TTS calls may be billed.",
        ),
    ] = False,
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Enable a configured automatic-delivery policy only with an explicit acknowledgement."""
    if not confirm_paid:
        typer.echo("ERROR: automation authorization requires --confirm-paid", err=True)
        raise typer.Exit(code=1)
    try:
        status = authorize_automation(_output_root(output_root))
    except ValueError as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        "Automation authorized: "
        f"retries_per_stage={status.policy.retries_per_stage} "
        f"policy={status.policy_sha256[:12]}"
    )


@automation_app.command("disable")
def automation_disable(
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Disable automatic paid delivery without deleting its local facts."""
    try:
        status = disable_automation(_output_root(output_root))
    except ValueError as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Automation disabled: policy={status.policy_sha256[:12]}")


@automation_app.command("status")
def automation_status(
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Show secret-free authorization, limits, and the latest tick summary."""
    root = _output_root(output_root)
    try:
        status = load_automation_status(root)
    except ValueError as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    if status is None:
        typer.echo("Automation: not configured")
        return
    policy = status.policy
    used, limit, remaining = provider_call_usage(
        root,
        status.policy_sha256,
        now=datetime.now(UTC),
        limit=policy.budget.provider_calls_per_day,
    )
    typer.echo(
        f"Automation: {'enabled' if policy.enabled else 'disabled'} "
        f"schedule={policy.schedule_id} "
        f"retries_per_stage={policy.retries_per_stage} "
        f"opportunities_per_stage={policy.retries_per_stage + 1} "
        f"max_items={policy.max_items_per_tick} policy={status.policy_sha256[:12]}"
    )
    typer.echo(
        "Daily provider calls (UTC, count not amount): "
        f"used={used} limit={limit} remaining={remaining}"
    )
    for state in list_task_states(root, status.policy_sha256):
        max_attempts = policy.retries_per_stage + 1
        stage_counts = " ".join(
            f"{stage}={sum(item.stage == stage for item in state.attempts)}/{max_attempts}"
            for stage in ("writer", "reviewer", "podcast", "tts")
        )
        reasons = ",".join(
            f"{item.stage}:{item.safe_summary or 'unknown'}"
            for item in state.attempts
            if item.status == "unknown"
        )
        reason = state.blocked_reason or ""
        if reasons:
            reason = f"{reason} unknown={reasons}" if reason else f"unknown={reasons}"
        typer.echo(f"Task {state.task_id}: {stage_counts} reason={reason or 'none'}")
    if status.last_tick_at is not None:
        typer.echo(
            f"Last tick: {status.last_tick_at.isoformat()} {status.last_tick_summary or ''}"
        )
    installation, exists = automation_scheduler_status(root)
    if installation is not None:
        typer.echo(
            f"Scheduler: {'installed' if exists else 'missing'} "
            f"task={installation.task_name} every={installation.interval_minutes}m"
        )


@automation_app.command("install")
def automation_install(
    every_minutes: Annotated[
        int,
        typer.Option("--every-minutes", min=1, max=1440, help="Wake-up interval."),
    ] = 30,
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Install or update the single Windows task that wakes `automation tick`."""
    root = _output_root(output_root)
    try:
        status = load_automation_status(root)
        if status is None:
            raise ValueError("automation is not configured")
        installation = install_automation_scheduler(
            root,
            policy_sha256=status.policy_sha256,
            interval_minutes=every_minutes,
        )
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"Automation scheduler installed: {installation.task_name} "
        f"every={installation.interval_minutes}m"
    )


@automation_app.command("uninstall")
def automation_uninstall(
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Remove only the Windows task recorded for this automatic-delivery policy."""
    try:
        removed = uninstall_automation_scheduler(_output_root(output_root))
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        "Automation scheduler removed"
        if removed
        else "Automation scheduler not installed"
    )


@automation_app.command("tick")
def automation_tick(
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Run one authorized monitor-to-audio tick; never changes foreground commands."""
    root = _output_root(output_root)
    secret: SecretStr | None = None
    try:
        status = load_automation_status(root)
        if status is None:
            raise ValueError("automation is not configured")
        if not status.policy.enabled or status.policy.authorized_at is None:
            raise ValueError("automation paid delivery is not authorized")
        tick_now = datetime.now(UTC)
        schedule = load_schedule(schedule_path(root, status.policy.schedule_id))
        if schedule.source.kind != "douyin":
            raise ValueError("automation policy schedule is not a Douyin monitor")
        douyin_cookie = _load_douyin_cookie(root)
        outcome = run_schedule_once(
            root,
            schedule.schedule_id,
            adapter_factory=_schedule_adapter,
            runtime_credentials=douyin_cookie,
        )
        if outcome.status not in {"completed", "partial"}:
            raise RuntimeError(f"monitor did not complete: {outcome.status}")
        downloads = run_pending_downloads(
            root,
            max_items=status.policy.max_items_per_tick,
            profile=schedule.profile,
            schedule_id=schedule.schedule_id,
            selection="oldest",
            retry_failed=True,
            max_stage_attempts=status.policy.retries_per_stage + 1,
            douyin_cookie=douyin_cookie,
            now=tick_now,
        )
        # The queue contains only retryable deterministic failures.  It runs
        # before any paid provider is even constructed for this tick.
        local_recoveries = run_failure_queue(
            root,
            workers=1,
            max_items=status.policy.max_items_per_tick,
            max_stage_attempts=status.policy.retries_per_stage + 1,
            now=tick_now,
        )
        runtime_environ = _runtime_environment()
        writer, secret = _assisted_note_provider(
            runtime_environ, root, status.policy.writer
        )
        reviewer, secret = _assisted_note_provider(
            runtime_environ, root, status.policy.reviewer
        )
        mimo_secret = _mimo_api_key(runtime_environ)
        _verify_provider_workers(runtime_environ)
        task_ids = tuple(
            dict.fromkeys(
                [
                    *list_incomplete_task_ids(root, status.policy_sha256),
                    *downloads.task_ids,
                ]
            )
        )
        result = run_automation_tasks(
            root,
            task_ids,
            AutomationProviders(
                writer=writer,
                reviewer=reviewer,
                podcast=MimoPodcastProvider(mimo_secret),
                tts=MimoTtsProvider(mimo_secret),
            ),
            now=tick_now,
        )
    except LockUnavailable as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, PipelineError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {_safe_error(error, secret)}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"Automation tick: claimed={downloads.claimed_count} tasks={len(result.task_ids)} "
        f"local_recovered={sum(item.status == 'completed' for item in local_recoveries)} "
        f"completed={len(result.completed_task_ids)} failed={len(result.failed_task_ids)}"
    )


def _verify_provider_workers(runtime_environ: Mapping[str, str]) -> list[str]:
    """Check installed ASR/OCR assets without forwarding any credentials."""
    provider_environment = {"HF_HUB_OFFLINE": "1"}
    cache = runtime_environ.get("HUGGINGFACE_HUB_CACHE", "").strip()
    if cache:
        provider_environment["HUGGINGFACE_HUB_CACHE"] = cache
    checked: list[str] = []
    for command in ("doctor-asr", "doctor-ocr"):
        result = providers.run_worker([command], environment=provider_environment)
        payload = json.loads(result.stdout)
        checked.append(str(payload["provider"]))
    return checked


@app.command()
def process(
    input_value: Annotated[
        str | None,
        typer.Argument(help="Local video path or public yt-dlp URL."),
    ] = None,
    profile: Annotated[
        ProfileOption,
        typer.Option(help="Pipeline profile: evidence, note, or full."),
    ] = ProfileOption.EVIDENCE,
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
    tasks_path: Annotated[
        Path | None,
        typer.Option("--tasks", help="TXT or JSONL source list."),
    ] = None,
    input_dir: Annotated[
        Path | None,
        typer.Option("--input-dir", help="Directory containing local videos."),
    ] = None,
    recursive: Annotated[
        bool,
        typer.Option("--recursive", help="Recursively scan --input-dir."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preflight without downloads or formal writes."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-run the canonical task as a force attempt."),
    ] = False,
    allow_duplicate: Annotated[
        bool,
        typer.Option(
            "--allow-duplicate",
            help="Allow duplicate content from a different source to create a task.",
        ),
    ] = False,
) -> None:
    """Process or preflight local, URL, task-file, or directory sources."""
    root = _output_root(output_root)
    try:
        sources = collect_sources(
            input_value=input_value,
            tasks_path=tasks_path,
            input_dir=input_dir,
            recursive=recursive,
        )
        if dry_run:
            result = preflight_sources(sources, root)
            typer.echo(f"DRY-RUN: {'OK' if result.ok else 'FAILED'}")
            for item in result.items:
                duplicate = " duplicate" if item.duplicate else ""
                typer.echo(f"- {item.task_id}: {item.planned_directory}{duplicate}")
            for issue in result.issues:
                typer.echo(
                    f"{issue.severity.upper()}: {issue.code}: {issue.message}",
                    err=issue.severity == "error",
                )
            if not result.ok:
                raise typer.Exit(code=1)
            return
        if tasks_path is not None or input_dir is not None:
            if force or allow_duplicate:
                raise ValueError("--force and --allow-duplicate require a single INPUT")
            manifest = run_batch(sources, root, profile.value)
            typer.echo(
                f"Processed batch: {manifest.batch_id} "
                f"completed={manifest.completed_count} "
                f"failed={manifest.failed_count} "
                f"skipped={manifest.skipped_count}"
            )
            return
        identity_options = (
            {"force": force, "allow_duplicate": allow_duplicate}
            if force or allow_duplicate
            else {}
        )
        if sources[0].input_type == "local_file":
            task = process_video(
                Path(sources[0].input),
                root,
                profile.value,
                **identity_options,
            )
        else:
            task = process_source(
                sources[0],
                root,
                profile.value,
                **identity_options,
            )
    except LockUnavailable as error:
        typer.echo("ERROR: task is still running", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, PipelineError, SourceParseError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Processed task: {task.task_id}")


@app.command()
def run(
    task_id: Annotated[str, typer.Argument(help="Stable task ID from task.json.")],
    from_stage: Annotated[
        StageOption,
        typer.Option("--from", help="Stage at which to resume processing."),
    ],
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """Resume a persisted task from one stage and rerun its downstream stages."""
    task_dir = _find_task_dir(task_id, _output_root(output_root))
    try:
        task = rerun_task(task_dir, from_stage.value)
    except LockUnavailable as error:
        typer.echo(f"ERROR: task is still running: {task_id}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, PipelineError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Processed task: {task.task_id}")


@app.command()
def recover(
    task_id: Annotated[str, typer.Argument(help="Stable task ID from task.json.")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report the recovery point without writing."),
    ] = False,
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """Resume a failed or interrupted deterministic task from its earliest invalid stage."""
    root = _output_root(output_root)
    task_dir = _find_task_dir(task_id, root)
    try:
        persisted = load_task(task_dir)
        with task_lock(root, persisted.task_id, timeout=0):
            plan = plan_recovery(task_dir)
            if plan.from_stage is None:
                typer.echo(f"No recovery needed: {task_id}")
                return
            typer.echo(
                f"Recovery plan: {task_id} from={plan.from_stage} "
                f"paid={str(plan.requires_paid).lower()} "
                f"reason={'; '.join(plan.reasons)}"
            )
            if dry_run:
                return
            if plan.requires_paid:
                typer.echo(
                    "ERROR: recovery reached a paid stage; "
                    "use the explicit note, podcast, or tts command",
                    err=True,
                )
                raise typer.Exit(code=1)
            task = rerun_task(
                task_dir,
                plan.from_stage,
                reason="resume",
                _task_lock_held=True,
            )
    except LockUnavailable as error:
        typer.echo(f"ERROR: task is still running: {task_id}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, PipelineError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Recovered task: {task.task_id}")


@web_app.command("serve")
def web_serve(
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
    port: Annotated[
        int,
        typer.Option(min=1, max=65535, help="Local loopback port."),
    ] = 8765,
) -> None:
    """Run the local, deterministic task workspace at 127.0.0.1."""
    serve_web_app(_output_root(output_root), port=port)


@app.command()
def retry(
    task_id: Annotated[str, typer.Argument(help="Stable task ID from task.json.")],
    from_stage: Annotated[
        str,
        typer.Option("--from", help="Stage to retry, or auto for recovery planning."),
    ] = "auto",
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """Append an explicit retry attempt without rewriting earlier attempt history."""
    root = _output_root(output_root)
    task_dir = _find_task_dir(task_id, root)
    try:
        persisted = load_task(task_dir)
        with task_lock(root, persisted.task_id, timeout=0):
            if from_stage == "auto":
                plan = plan_recovery(task_dir)
                selected = plan.from_stage
                requires_paid = plan.requires_paid
            else:
                if from_stage not in STAGES:
                    raise typer.BadParameter(
                        f"unknown stage: {from_stage}", param_hint="--from"
                    )
                selected = from_stage
                requires_paid = selected in {"note", "podcast_script", "tts"}
            if selected is None:
                typer.echo(f"No retry needed: {task_id}")
                return
            if requires_paid:
                typer.echo(
                    "ERROR: retry reached a paid stage; "
                    "use the explicit note, podcast, or tts command",
                    err=True,
                )
                raise typer.Exit(code=1)
            task = rerun_task(
                task_dir,
                selected,
                reason="retry",
                _task_lock_held=True,
            )
    except LockUnavailable as error:
        typer.echo(f"ERROR: task is still running: {task_id}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, PipelineError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Retried task: {task.task_id}")


@batch_app.command("resume")
def resume_batch_command(
    batch_id: Annotated[str, typer.Argument(help="Stable batch ID from batch.json.")],
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """Resume pending or interrupted items without repeating final items."""
    root = _output_root(output_root)
    batch_dir = root / "视频学习批次" / batch_id
    if not (batch_dir / "batch.json").is_file():
        raise typer.BadParameter(f"batch not found: {batch_id}", param_hint="batch_id")
    try:
        manifest = resume_batch(batch_dir)
    except LockUnavailable as error:
        typer.echo(f"ERROR: batch is still running: {batch_id}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, PipelineError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"Resumed batch: {manifest.batch_id} "
        f"completed={manifest.completed_count} "
        f"failed={manifest.failed_count} "
        f"skipped={manifest.skipped_count}"
    )


@index_app.command("rebuild")
def index_rebuild(
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """Rebuild SQLite from task, batch, and schedule fact files."""
    try:
        result = rebuild_index(_output_root(output_root))
    except LockUnavailable as error:
        typer.echo("ERROR: index rebuild is already running", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, IndexRebuildError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"Rebuilt index: tasks={result.task_count} attempts={result.attempt_count} "
        f"batches={result.batch_count} schedules={result.schedule_count} "
        f"path={result.database_path}"
    )


@index_app.command("status")
def show_index_status(
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """Report whether the SQLite projection matches current fact files."""
    status = index_status(_output_root(output_root))
    typer.echo(
        f"Index status: exists={str(status.exists).lower()} "
        f"stale={str(status.stale).lower()} tasks={status.task_count} "
        f"attempts={status.attempt_count} batches={status.batch_count} "
        f"schedules={status.schedule_count} "
        f"reason={status.reason}"
    )
    if status.stale:
        raise typer.Exit(code=1)


@app.command()
def history(
    task_id: Annotated[
        str | None,
        typer.Option("--task-id", help="Limit history to one stable task ID."),
    ] = None,
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """Query task and immutable attempt history from the SQLite projection."""
    try:
        rows = query_history(_output_root(output_root), task_id=task_id)
    except (OSError, sqlite3.Error, IndexRebuildError) as index_error:
        try:
            rows = query_history_facts(_output_root(output_root), task_id=task_id)
        except (OSError, ValueError, IndexRebuildError) as fact_error:
            typer.echo(f"ERROR: {fact_error}", err=True)
            raise typer.Exit(code=1) from fact_error
        typer.echo(
            f"WARNING: index unavailable ({index_error}); using task facts",
            err=True,
        )
    if not rows:
        typer.echo("No history entries.")
        return
    for row in rows:
        typer.echo(
            f"{row['task_id']} attempt={row['ordinal'] or '-'} "
            f"status={row['attempt_status'] or '-'} "
            f"from={row['from_stage'] or '-'} title={row['title']}"
        )


@app.command()
def status(
    task_id: Annotated[str, typer.Argument(help="Stable task ID from task.json.")],
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """Report one task directly from its fact file, without SQLite dependency."""
    root = _output_root(output_root)
    try:
        task_dir = _find_task_dir(task_id, root, include_invalid=True)
        task = load_task(task_dir)
    except (OSError, ValueError, typer.BadParameter) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    stages = " ".join(f"{name}={value}" for name, value in task.stages.items())
    active = " ".join(
        f"{stage}:{paths[0]}" for stage, paths in task.artifacts.items() if paths
    )
    typer.echo(f"{task.task_id} title={task.title}")
    typer.echo(f"stages: {stages}")
    typer.echo(f"active-artifacts: {active or '-'}")
    typer.echo(
        f"attempts={len(task.attempts)} active_attempt={task.active_attempt_id or '-'}"
    )


@layout_app.command("plan")
def layout_plan(
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """Preview the compatibility-preserving delivery/audit directory migration."""
    moves = plan_artifact_layout(_output_root(output_root))
    if not moves:
        typer.echo("Artifact layout already has no legacy roots to migrate.")
        return
    for move in moves:
        typer.echo(f"MOVE {move.source} -> {move.destination}")
    typer.echo("Legacy paths will become directory aliases; no files are copied.")


@layout_app.command("migrate")
def layout_migrate(
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Acknowledge the physical directory move."),
    ] = False,
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """Move legacy roots once and preserve their old paths as directory aliases."""
    if not confirm:
        typer.echo("ERROR: layout migration requires --confirm", err=True)
        raise typer.Exit(code=1)
    try:
        moves = migrate_artifact_layout(_output_root(output_root))
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    if not moves:
        typer.echo("Artifact layout already has no legacy roots to migrate.")
        return
    typer.echo(f"Migrated artifact roots: {len(moves)}")


@queue_app.command("list")
def queue_list(
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """List due retryable failures; this command never executes paid work."""
    try:
        rows = query_failure_queue(_output_root(output_root))
    except (OSError, sqlite3.Error, IndexRebuildError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    if not rows:
        typer.echo("Failure queue is empty.")
        return
    for row in rows:
        typer.echo(
            f"{row['task_id']} attempt={row['ordinal']} "
            f"stage={row['failed_stage']} code={row['failure_code']} "
            f"next={row['next_retry_at'] or 'now'}"
        )


@queue_app.command("run")
def queue_run(
    workers: Annotated[
        int,
        typer.Option("--workers", min=1, help="Concurrent free recovery workers."),
    ] = 2,
    max_items: Annotated[
        int,
        typer.Option("--max-items", min=1, help="Maximum due items to claim."),
    ] = 10,
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """Run due retryable deterministic stages; never invoke paid providers."""
    try:
        results = run_failure_queue(
            _output_root(output_root),
            workers=workers,
            max_items=max_items,
        )
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        IndexRebuildError,
        LockUnavailable,
    ) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    if not results:
        typer.echo("Failure queue is empty.")
        return
    for item in results:
        typer.echo(
            f"{item.task_id}: {item.status}"
            + (f" ({item.error})" if item.error else "")
        )
    if any(item.status != "completed" for item in results):
        raise typer.Exit(code=1)


@download_app.command("pending")
def download_pending(
    max_items: Annotated[
        int,
        typer.Option("--max-items", min=1, help="Maximum discovered links to claim."),
    ] = 10,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Retry previously retryable downloads."),
    ] = False,
    schedule_id: Annotated[
        str | None,
        typer.Option("--schedule-id", help="Consume links from one monitor only."),
    ] = None,
    latest_only: Annotated[
        bool,
        typer.Option(
            "--latest-only",
            help="Test selector: consume only the newest link from this fresh observation.",
        ),
    ] = False,
    profile: Annotated[
        ProfileOption,
        typer.Option(help="Pipeline profile for downloaded links."),
    ] = ProfileOption.EVIDENCE,
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", help="Vault root."),
    ] = None,
) -> None:
    """Download persisted monitor links without running the monitor.

    The normal queue retains every discovered item. ``--latest-only`` is only
    for one-shot verification and never changes monitor cursor semantics.
    """
    try:
        if latest_only and schedule_id is None:
            raise ValueError("--latest-only requires --schedule-id")
        root = _output_root(output_root)
        outcome = run_pending_downloads(
            root,
            max_items=1 if latest_only else max_items,
            retry_failed=retry_failed,
            schedule_id=schedule_id,
            selection="latest_observed" if latest_only else "oldest",
            profile=profile.value,
            douyin_cookie=_load_douyin_cookie(root),
        )
    except (LockUnavailable, OSError, PipelineError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    if outcome.claimed_count == 0:
        typer.echo("Pending download queue is empty.")
        return
    typer.echo(
        f"Downloaded links: batch={outcome.batch_id} "
        f"claimed={outcome.claimed_count} "
        f"completed={outcome.completed_count} failed={outcome.failed_count}"
    )
    if outcome.failed_count:
        raise typer.Exit(code=1)


@app.command()
def validate(
    task_id: Annotated[str, typer.Argument(help="Stable task ID from task.json.")],
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
) -> None:
    """Validate one task's persisted artifacts and evidence references."""
    task_dir = _find_task_dir(task_id, _output_root(output_root), include_invalid=True)
    errors = validate_task(task_dir)
    if errors:
        for error in errors:
            typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Valid task: {task_id}")


@quality_note_app.command("plan")
def quality_note_plan(
    task_ids: Annotated[
        list[str],
        typer.Argument(help="One or more stable task IDs with content packs."),
    ],
    template: Annotated[
        str,
        typer.Option(
            "--template", help="Reader template type or a constrained JSON file."
        ),
    ] = "mixed",
    review_mode: Annotated[
        QualityReviewModeOption,
        typer.Option(
            "--review-mode", help="Quality review policy: none, report, or gate."
        ),
    ] = QualityReviewModeOption.NONE,
    shard_size: Annotated[
        int,
        typer.Option(
            "--shard-size", min=1, help="Maximum source atoms per Organizer shard."
        ),
    ] = 80,
    reuse_organization: Annotated[
        Path | None,
        typer.Option(
            "--reuse-organization",
            help="Validated organization.json to reuse for one matching task.",
        ),
    ] = None,
    connection: Annotated[
        str | None,
        typer.Option(
            "--connection",
            help="Verified provider connection; defaults to the configured default.",
        ),
    ] = None,
    organizer_provider: Annotated[
        str,
        typer.Option("--organizer-provider", help="Persisted Organizer provider name."),
    ] = "xiaomi-mimo",
    organizer_model: Annotated[
        str, typer.Option("--organizer-model", help="Persisted Organizer model name.")
    ] = "mimo-v2.5",
    writer_provider: Annotated[
        str, typer.Option("--writer-provider", help="Persisted Writer provider name.")
    ] = "xiaomi-mimo",
    writer_model: Annotated[
        str, typer.Option("--writer-model", help="Persisted Writer model name.")
    ] = "mimo-v2.5",
    reviewer_provider: Annotated[
        str,
        typer.Option("--reviewer-provider", help="Persisted Reviewer provider name."),
    ] = "xiaomi-mimo",
    reviewer_model: Annotated[
        str, typer.Option("--reviewer-model", help="Persisted Reviewer model name.")
    ] = "mimo-v2.5",
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Write an immutable quality-first execution plan without provider calls."""
    root = _output_root(output_root)
    try:
        selected_connection = get_connection(root, connection)
        profile = profile_for_connection(root, selected_connection)
        if (
            profile is None
            or profile.status != "verified"
            or profile.strategy is None
            or profile.extractor is None
        ):
            raise ValueError(
                "Writer capability is unchecked; run provider check explicitly"
            )
        snapshot = WriterCapabilitySnapshot(
            profile_id=profile.profile_id,
            profile_sha256=profile.profile_sha256,
            provider=profile.provider,
            endpoint_identity=profile.endpoint_identity,
            model=profile.model,
            adapter_revision=profile.adapter_revision,
            strategy=profile.strategy,
            extractor=profile.extractor,
        )
        if writer_provider == "xiaomi-mimo" and writer_model == "mimo-v2.5":
            writer_provider, writer_model = (
                selected_connection.provider,
                selected_connection.model,
            )
        if organizer_provider == "xiaomi-mimo" and organizer_model == "mimo-v2.5":
            organizer_provider, organizer_model = (
                selected_connection.provider,
                selected_connection.model,
            )
        if reviewer_provider == "xiaomi-mimo" and reviewer_model == "mimo-v2.5":
            reviewer_provider, reviewer_model = (
                selected_connection.provider,
                selected_connection.model,
            )
        if (
            writer_provider != selected_connection.provider
            or writer_model != selected_connection.model
        ):
            raise ValueError(
                "quality plan Writer provider/model must match the selected connection"
            )
        selected_template = resolve_reader_template(template, default_type="mixed")
        plan_path = create_quality_plan(
            root,
            task_ids,
            template=selected_template,
            organizer_provider=organizer_provider,
            organizer_model=organizer_model,
            writer_provider=writer_provider,
            writer_model=writer_model,
            writer_capability=snapshot,
            reviewer_provider=(
                reviewer_provider
                if review_mode is not QualityReviewModeOption.NONE
                else None
            ),
            reviewer_model=(
                reviewer_model
                if review_mode is not QualityReviewModeOption.NONE
                else None
            ),
            review_mode=review_mode.value,
            shard_size=shard_size,
            reuse_organization_path=reuse_organization,
        )
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Quality plan: {plan_path}")


@quality_note_app.command("organize")
def quality_note_organize(
    plan_path: Annotated[
        Path, typer.Argument(exists=True, file_okay=True, dir_okay=True, readable=True)
    ],
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Execute the planned Organizer calls exactly once per missing shard."""
    root = _output_root(output_root)
    secret: SecretStr | None = None
    try:
        provider, secret = _quality_note_provider(
            _runtime_environment(), output_root=root
        )
        with _resource_execution(root, "network", "llm"):
            state = organize_quality_plan(plan_path, root, provider)
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {_safe_error(error, secret)}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(_quality_state_summary(state))


@quality_note_app.command("generate")
def quality_note_generate(
    plan_path: Annotated[
        Path, typer.Argument(exists=True, file_okay=True, dir_okay=True, readable=True)
    ],
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Execute the planned Writer calls after organization bundles are complete."""
    root = _output_root(output_root)
    secret: SecretStr | None = None
    try:
        provider, secret = _quality_note_provider(
            _runtime_environment(), output_root=root
        )
        with _resource_execution(root, "network", "llm"):
            state = generate_quality_note_plan(plan_path, root, provider)
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {_safe_error(error, secret)}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(_quality_state_summary(state))


@quality_note_app.command("review")
def quality_note_review(
    plan_path: Annotated[
        Path, typer.Argument(exists=True, file_okay=True, dir_okay=True, readable=True)
    ],
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Run the planned Reviewer or local quality report and apply gate semantics."""
    root = _output_root(output_root)
    secret: SecretStr | None = None
    try:
        plan = load_quality_plan(plan_path)
        needs_reviewer = any(item.review_mode != "none" for item in plan.tasks)
        provider = None
        if needs_reviewer:
            provider, secret = _quality_note_provider(
                _runtime_environment(), output_root=root
            )
        if provider is None:
            state = review_quality_note_plan(plan_path, root)
        else:
            with _resource_execution(root, "network", "llm"):
                state = review_quality_note_plan(plan_path, root, provider)
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {_safe_error(error, secret)}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(_quality_state_summary(state))


@quality_note_app.command("status")
def quality_note_status(
    plan_path: Annotated[
        Path, typer.Argument(exists=True, file_okay=True, dir_okay=True, readable=True)
    ],
) -> None:
    """Show persisted quality-first status and actual role call counts."""
    try:
        state = load_quality_state(plan_path)
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(_quality_state_summary(state))


@quality_note_app.command("recover")
def quality_note_recover(
    plan_path: Annotated[
        Path, typer.Argument(exists=True, file_okay=True, dir_okay=True, readable=True)
    ],
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Recover only local quality bundle writes and publication, never provider calls."""
    root = _output_root(output_root)
    try:
        state = recover_quality_plan(plan_path, root)
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(_quality_state_summary(state))


@assisted_note_app.command("plan")
def assisted_note_plan(
    task_ids: Annotated[
        list[str],
        typer.Argument(help="One or more stable task IDs with content packs."),
    ],
    connection: Annotated[
        str | None,
        typer.Option(
            "--connection", help="Writer connection; defaults to configured default."
        ),
    ] = None,
    reviewer_connection: Annotated[
        str | None,
        typer.Option(
            "--reviewer-connection", help="Optional distinct Reviewer connection."
        ),
    ] = None,
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Write an immutable assisted-draft plan without provider calls or checks."""
    root = _output_root(output_root)
    try:
        writer = _assisted_connection_snapshot(get_connection(root, connection))
        reviewer = _assisted_connection_snapshot(
            get_connection(root, reviewer_connection or connection)
        )
        plan_path = create_assisted_plan(
            root, task_ids, writer=writer, reviewer=reviewer
        )
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Assisted plan: {plan_path}")


@assisted_note_app.command("generate")
def assisted_note_generate(
    plan_path: Annotated[
        Path, typer.Argument(exists=True, file_okay=True, dir_okay=True, readable=True)
    ],
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Execute each planned Markdown Writer once; no retry is implicit."""
    root = _output_root(output_root)
    secret: SecretStr | None = None
    try:
        plan = load_assisted_plan(plan_path)
        provider, secret = _assisted_note_provider(
            _runtime_environment(), root, plan.writer
        )
        with _resource_execution(root, "network", "llm"):
            state = generate_assisted_plan(plan_path, root, provider)
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {_safe_error(error, secret)}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(_assisted_state_summary(state))


@assisted_note_app.command("review")
def assisted_note_review(
    plan_path: Annotated[
        Path, typer.Argument(exists=True, file_okay=True, dir_okay=True, readable=True)
    ],
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Execute each planned Markdown Reviewer once after a saved candidate exists."""
    root = _output_root(output_root)
    secret: SecretStr | None = None
    try:
        plan = load_assisted_plan(plan_path)
        provider, secret = _assisted_note_provider(
            _runtime_environment(), root, plan.reviewer
        )
        with _resource_execution(root, "network", "llm"):
            state = review_assisted_plan(plan_path, root, provider)
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {_safe_error(error, secret)}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(_assisted_state_summary(state))


@assisted_note_app.command("status")
def assisted_note_status(
    plan_path: Annotated[
        Path, typer.Argument(exists=True, file_okay=True, dir_okay=True, readable=True)
    ],
) -> None:
    """Show assisted-draft status and explicit Writer/Reviewer call counts."""
    try:
        state = load_assisted_state(plan_path)
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(_assisted_state_summary(state))


@assisted_note_app.command("recover")
def assisted_note_recover(
    plan_path: Annotated[
        Path, typer.Argument(exists=True, file_okay=True, dir_okay=True, readable=True)
    ],
    output_root: Annotated[
        Path | None, typer.Option("--output-root", help="Vault root.")
    ] = None,
) -> None:
    """Repair only persisted assisted-draft files; never call a provider."""
    root = _output_root(output_root)
    try:
        state = recover_assisted_plan(plan_path, root)
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(_assisted_state_summary(state))


@app.command()
def podcast(
    task_id: Annotated[str, typer.Argument(help="Stable task ID from task.json.")],
    external_script: Annotated[
        Path | None,
        typer.Option(
            "--external-script",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Validated PodcastScript 1.0 JSON from an external Agent.",
        ),
    ] = None,
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
    retain_debug_artifacts: Annotated[
        bool,
        typer.Option(
            "--retain-debug-artifacts",
            help="Keep provider raw responses in this generated podcast bundle.",
        ),
    ] = False,
) -> None:
    """Generate and activate a validated podcast script explicitly."""
    root = _output_root(output_root)
    task_dir = _find_task_dir(task_id, root)
    secret: SecretStr | None = None
    debug_options = {"retain_debug_artifacts": True} if retain_debug_artifacts else {}
    try:
        with _task_execution_lock(root, task_dir):
            if external_script is not None:
                if retain_debug_artifacts:
                    raise ValueError(
                        "--external-script and --retain-debug-artifacts are mutually exclusive"
                    )
                task = build_and_activate_external_podcast(
                    task_dir,
                    external_script,
                    root,
                )
            else:
                secret = _mimo_api_key(_runtime_environment())
                with _resource_execution(root, "network", "llm"):
                    task = generate_and_activate_podcast(
                        task_dir,
                        MimoPodcastProvider(secret),
                        root,
                        **debug_options,
                    )
    except LockUnavailable as error:
        typer.echo(f"ERROR: task is still running: {task_id}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {_safe_error(error, secret)}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Generated podcast: {task.task_id}")


@app.command()
def tts(
    task_id: Annotated[str, typer.Argument(help="Stable task ID from task.json.")],
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root", help="Vault root; defaults to the current directory."
        ),
    ] = None,
    style: Annotated[
        str,
        typer.Option("--style", help="Natural-language speech style instruction."),
    ] = DEFAULT_TTS_STYLE,
) -> None:
    """Synthesize the active podcast speech into validated WAV and MP3."""
    root = _output_root(output_root)
    task_dir = _find_task_dir(task_id, root)
    secret: SecretStr | None = None
    typer.echo("正在请求 MiMo TTS，服务端可能需要几分钟；请勿重复执行。")
    try:
        with _task_execution_lock(root, task_dir):
            secret = _mimo_api_key(_runtime_environment())
            with _resource_execution(root, "network", "tts", "ffmpeg"):
                task = generate_and_activate_tts(
                    task_dir,
                    MimoTtsProvider(secret),
                    root,
                    style_instruction=style,
                )
    except LockUnavailable as error:
        typer.echo(f"ERROR: task is still running: {task_id}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, RuntimeError, ValueError) as error:
        typer.echo(f"ERROR: {_safe_error(error, secret)}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Generated audio: {task.task_id}")


@scan_app.command("folder")
def scan_folder_command(
    path: Annotated[Path, typer.Argument(help="Folder containing local videos.")],
    recursive: Annotated[
        bool,
        typer.Option("--recursive", help="Include nested folders."),
    ] = False,
    profile: Annotated[
        ProfileOption,
        typer.Option(help="Pipeline profile for newly discovered sources."),
    ] = ProfileOption.EVIDENCE,
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", help="Vault root."),
    ] = None,
) -> None:
    """Run one recoverable incremental folder scan in the foreground."""
    root = _output_root(output_root)
    try:
        adapter = FolderAdapter(path, recursive=recursive)
        schedule = ensure_manual_folder_schedule(
            root,
            adapter,
            profile=profile.value,
        )
        outcome = run_schedule_once(root, schedule.schedule_id, adapter=adapter)
        manifest = (
            load_batch(root / "视频学习批次" / outcome.batch_id)
            if outcome.batch_id is not None
            else None
        )
    except LockUnavailable as error:
        typer.echo(f"ERROR: scan is already running: {error}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, PipelineError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    added = len(manifest.results) if manifest is not None else 0
    typer.echo(
        f"Scanned folder: {schedule.schedule_id} status={outcome.status} added={added}"
    )
    if outcome.status not in {"completed", "partial"}:
        raise typer.Exit(code=1)


@schedule_app.command("add-folder")
def schedule_add_folder_command(
    schedule_id: Annotated[str, typer.Argument(help="Stable schedule ID.")],
    path: Annotated[Path, typer.Argument(help="Folder containing local videos.")],
    every_minutes: Annotated[
        int,
        typer.Option("--every-minutes", min=1, help="Foreground tick interval."),
    ] = 60,
    recursive: Annotated[
        bool,
        typer.Option("--recursive", help="Include nested folders."),
    ] = False,
    profile: Annotated[
        ProfileOption,
        typer.Option(help="Pipeline profile for newly discovered sources."),
    ] = ProfileOption.EVIDENCE,
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", help="Vault root."),
    ] = None,
) -> None:
    """Persist one interval definition; it runs only via `schedule tick`."""
    root = _output_root(output_root)
    try:
        schedule = create_interval_folder_schedule(
            root,
            schedule_id,
            path,
            every_seconds=every_minutes * 60,
            recursive=recursive,
            profile=profile.value,
        )
    except (OSError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Added schedule: {schedule.schedule_id}")


@schedule_app.command("add-douyin")
def schedule_add_douyin_command(
    schedule_id: Annotated[str, typer.Argument(help="Stable schedule ID.")],
    url: Annotated[str, typer.Argument(help="Douyin favorites page URL.")],
    every_minutes: Annotated[
        int,
        typer.Option("--every-minutes", min=1, help="Foreground tick interval."),
    ] = 60,
    profile: Annotated[
        ProfileOption,
        typer.Option(help="Pipeline profile for newly discovered sources."),
    ] = ProfileOption.EVIDENCE,
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", help="Vault root."),
    ] = None,
) -> None:
    """Persist a default-video Douyin favorites monitor."""
    root = _output_root(output_root)
    try:
        schedule = create_interval_douyin_schedule(
            root,
            schedule_id,
            url,
            every_seconds=every_minutes * 60,
            profile=profile.value,
        )
    except (OSError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Added schedule: {schedule.schedule_id}")


@schedule_app.command("list")
def schedule_list_command(
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", help="Vault root."),
    ] = None,
) -> None:
    """List project-internal schedule facts."""
    root = _output_root(output_root)
    try:
        schedules = list_schedules(root)
    except (OSError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    for schedule in schedules:
        typer.echo(
            f"{schedule.schedule_id}\t{schedule.status}\t"
            f"{schedule.source.kind}\t{schedule.trigger.kind}"
        )


@schedule_app.command("run")
def schedule_run_command(
    schedule_id: Annotated[str, typer.Argument(help="Stable schedule ID.")],
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", help="Vault root."),
    ] = None,
) -> None:
    """Run exactly one monitor in this foreground process, regardless of due time."""
    root = _output_root(output_root)
    try:
        runtime_credentials = _load_douyin_cookie(root)
        outcome = run_schedule_once(
            root,
            schedule_id,
            adapter_factory=_schedule_adapter,
            runtime_credentials=runtime_credentials,
        )
    except LockUnavailable as error:
        typer.echo(f"ERROR: schedule is already running: {schedule_id}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, PipelineError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"{outcome.schedule_id}: {outcome.status}"
        + (f" batch={outcome.batch_id}" if outcome.batch_id else "")
        + (f" ({outcome.error})" if outcome.error else "")
    )
    if outcome.status not in {"completed", "partial"}:
        raise typer.Exit(code=1)


@schedule_app.command("disable")
def schedule_disable_command(
    schedule_id: Annotated[str, typer.Argument(help="Stable schedule ID.")],
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", help="Vault root."),
    ] = None,
) -> None:
    """Disable one project-internal schedule without deleting its fact."""
    root = _output_root(output_root)
    try:
        with schedule_lock(root, schedule_id, timeout=0):
            current = load_schedule(schedule_path(root, schedule_id))
            updated = current.model_copy(update={"status": "disabled"})
            write_schedule_atomic(root, updated)
    except LockUnavailable as error:
        typer.echo(f"ERROR: schedule is already running: {schedule_id}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Disabled schedule: {schedule_id}")


@schedule_app.command("tick")
def schedule_tick_command(
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", help="Vault root."),
    ] = None,
) -> None:
    """Run due schedules once in this foreground process."""
    root = _output_root(output_root)
    try:
        runtime_credentials = _load_douyin_cookie(root)
        outcomes = tick_schedules(
            root,
            adapter_factory=_schedule_adapter,
            runtime_credentials=runtime_credentials,
        )
    except LockUnavailable as error:
        typer.echo(f"ERROR: schedule is already running: {error}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, PipelineError, ValueError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error
    failed = False
    for outcome in outcomes:
        typer.echo(
            f"{outcome.schedule_id}: {outcome.status}"
            + (f" ({outcome.error})" if outcome.error else "")
        )
        failed = failed or outcome.status in {"blocked", "failed", "interrupted"}
    if failed:
        raise typer.Exit(code=1)


def _schedule_adapter(schedule, credentials):
    if schedule.source.kind == "folder":
        return FolderAdapter(
            schedule.source.path,
            recursive=schedule.source.recursive,
        )
    if credentials is None or not credentials.get_secret_value().strip():
        raise RuntimeError(
            "Douyin login is missing; connect this output root in WebUI "
            "or provide legacy DOUYIN_COOKIE"
        )
    if schedule.source.folder_ids:
        raise RuntimeError(
            "custom Douyin folder monitoring is not verified on the pure HTTP path"
        )
    transport = DouyinHttpTransport(credentials)
    return DouyinFavoritesAdapter(
        transport,
        folder_ids=(),
        include_default_video=schedule.source.include_default_video,
    )


def _load_douyin_cookie(output_root: Path | None = None) -> SecretStr | None:
    """Resolve one runtime CookieJar without creating a second fact source.

    A WebUI login encrypted under the selected output root is canonical.
    ``DOUYIN_COOKIE`` remains a compatibility fallback only when that local
    encrypted credential does not exist.
    """
    if output_root is not None:
        persisted = DouyinCookieStore(output_root).load()
        if persisted is not None:
            return persisted
    value = _runtime_environment().get("DOUYIN_COOKIE", "")
    return SecretStr(value) if value else None


def _runtime_environment() -> dict[str, str]:
    """Read whitelisted local runtime settings without storing their values."""
    return load_runtime_environment(Path.cwd())


def _output_root(output_root: Path | None) -> Path:
    """Resolve the optional vault root at command execution time."""
    return (output_root if output_root is not None else Path.cwd()).resolve()


def _task_execution_lock(root: Path, task_dir: Path):
    persisted = load_task(task_dir)
    return task_lock(root, persisted.task_id, timeout=0)


@contextmanager
def _resource_execution(root: Path, *resources: str):
    scheduler = ResourceScheduler(root)
    with ExitStack() as stack:
        for resource in resources:
            stack.enter_context(scheduler.acquire(resource))
        yield


def _find_task_dir(
    task_id: str, output_root: Path, *, include_invalid: bool = False
) -> Path:
    """Find one persisted task by stable ID without relying on its mutable title."""
    tasks_root = output_root / "视频学习素材"
    matches: list[Path] = []
    for task_json in tasks_root.glob("*/task.json"):
        try:
            task = load_task(task_json.parent)
        except (OSError, ValueError):
            if include_invalid and _raw_task_id(task_json) == task_id:
                matches.append(task_json.parent)
            continue
        if task.task_id == task_id:
            matches.append(task_json.parent)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise typer.BadParameter(
            f"multiple task directories found: {task_id}", param_hint="task_id"
        )
    raise typer.BadParameter(f"task not found: {task_id}", param_hint="task_id")


def _raw_task_id(task_json: Path) -> str | None:
    """Read only the raw task identity so validation can report schema errors."""
    try:
        payload = json.loads(task_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("task_id") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


def _mimo_api_key(environ: Mapping[str, str]) -> SecretStr:
    value = environ.get("MIMO_API_KEY", "").strip()
    if not value:
        raise ValueError("MIMO_API_KEY is missing")
    return SecretStr(value)


def _note_safe_input_tokens(environ: Mapping[str, str]) -> int:
    """Read one transparent conservative budget for note provider inputs."""
    raw = environ.get("LEARNNEST_NOTE_SAFE_INPUT_TOKENS", "").strip()
    if not raw:
        return DEFAULT_NOTE_SAFE_INPUT_TOKENS
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(
            "LEARNNEST_NOTE_SAFE_INPUT_TOKENS must be an integer"
        ) from error
    if value < 1_024:
        raise ValueError("LEARNNEST_NOTE_SAFE_INPUT_TOKENS must be at least 1024")
    return value


def _quality_note_provider(
    environ: Mapping[str, str],
    *,
    output_root: Path | None = None,
) -> tuple[object, SecretStr]:
    """Build the explicit quality-role provider from the same BYOK boundary."""
    safe_input_tokens = _note_safe_input_tokens(environ)
    if output_root is not None:
        try:
            connection = get_connection(output_root)
        except ValueError:
            connection = None
        if connection is not None:
            if connection.api_family != "openai_chat":
                raise ValueError(
                    "native provider quality adapters are not available in this build"
                )
            profile = profile_for_connection(output_root, connection)
            if profile is None or profile.status != "verified":
                raise ValueError(
                    "Writer capability is unchecked; run provider check explicitly"
                )
            secret_value = environ.get(connection.secret_env, "").strip()
            if not secret_value:
                raise ValueError(f"{connection.secret_env} is missing")
            config = OpenAICompatibleChatConfig(
                provider_name=connection.provider,
                model=connection.model,
                base_url=connection.endpoint,
                api_key=SecretStr(secret_value),
                writer_capability=profile,
                safe_input_tokens=safe_input_tokens,
            )
            return OpenAICompatibleQualityNoteProvider(config), config.api_key
    generic_names = (
        "LEARNNEST_NOTE_API_KEY",
        "LEARNNEST_NOTE_BASE_URL",
        "LEARNNEST_NOTE_MODEL",
    )
    if any(name in environ for name in generic_names):
        values = {name: environ.get(name, "").strip() for name in generic_names}
        if not all(values.values()):
            raise ValueError(
                "LEARNNEST_NOTE_API_KEY, LEARNNEST_NOTE_BASE_URL, and "
                "LEARNNEST_NOTE_MODEL must be set together"
            )
        if environ.get("MIMO_API_KEY", "").strip():
            raise ValueError(
                "generic note provider configuration cannot be combined with "
                "MIMO_API_KEY"
            )
        config = OpenAICompatibleChatConfig(
            provider_name=environ.get("LEARNNEST_NOTE_PROVIDER", "").strip()
            or "openai-compatible",
            model=values["LEARNNEST_NOTE_MODEL"],
            base_url=values["LEARNNEST_NOTE_BASE_URL"],
            api_key=SecretStr(values["LEARNNEST_NOTE_API_KEY"]),
            json_response_mode=environ.get(
                "LEARNNEST_NOTE_JSON_MODE", "json_object"
            ).strip()
            or "json_object",
            writer_strategy_override=_writer_strategy_override(environ),
            safe_input_tokens=safe_input_tokens,
        )
        return OpenAICompatibleQualityNoteProvider(config), config.api_key
    secret = _mimo_api_key(environ)
    return MimoQualityNoteProvider(secret, safe_input_tokens=safe_input_tokens), secret


def _writer_strategy_override(
    environ: Mapping[str, str],
) -> str | None:
    explicit_strategy = environ.get("LEARNNEST_NOTE_WRITER_STRATEGY", "").strip()
    if explicit_strategy:
        return explicit_strategy
    legacy_mode = environ.get("LEARNNEST_NOTE_JSON_MODE", "").strip()
    return {
        "json_schema": "native_json_schema",
        "json_object": "json_object",
        "prompt_only": "prompted_json",
    }.get(legacy_mode)


def _write_local_env_value(path: Path, key: str, value: str) -> None:
    """Replace one local .env value without printing or persisting it elsewhere."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    replaced = False
    result: list[str] = []
    for line in lines:
        if line.split("=", 1)[0].strip().removeprefix("export ").strip() == key:
            result.append(f"{key}={value}")
            replaced = True
        else:
            result.append(line)
    if not replaced:
        result.append(f"{key}={value}")
    path.write_text("\n".join(result) + "\n", encoding="utf-8")


def _safe_error(error: Exception, secret: SecretStr | None) -> str:
    message = str(error)
    if secret is not None:
        value = secret.get_secret_value()
        if value:
            message = message.replace(value, "***")
    return message


def _assisted_connection_snapshot(
    connection: ProviderConnection,
) -> AssistedConnectionSnapshot:
    if connection.api_family != "openai_chat":
        raise ValueError(
            "native provider assisted adapters are not available in this build"
        )
    return AssistedConnectionSnapshot(
        connection_name=connection.name,
        provider=connection.provider,
        endpoint_identity=connection.endpoint.strip().rstrip("/").lower(),
        model=connection.model,
        adapter_revision=connection.adapter_revision,
    )


def _assisted_note_provider(
    environ: Mapping[str, str],
    output_root: Path,
    snapshot: AssistedConnectionSnapshot,
) -> tuple[OpenAICompatibleAssistedNoteProvider, SecretStr]:
    connection = get_connection(output_root, snapshot.connection_name)
    current = _assisted_connection_snapshot(connection)
    if current != snapshot:
        raise ValueError(
            "assisted provider connection no longer matches the immutable plan"
        )
    secret_value = environ.get(connection.secret_env, "").strip()
    if not secret_value:
        raise ValueError(f"{connection.secret_env} is missing")
    config = OpenAICompatibleChatConfig(
        provider_name=connection.provider,
        model=connection.model,
        base_url=connection.endpoint,
        api_key=SecretStr(secret_value),
        safe_input_tokens=_note_safe_input_tokens(environ),
    )
    return OpenAICompatibleAssistedNoteProvider(config), config.api_key


def _quality_state_summary(state: object) -> str:
    rows: list[str] = []
    for task in state.tasks:  # type: ignore[attr-defined]
        rows.append(
            f"{task.task_id}: {task.status} "
            f"organizer={task.organizer.actual_call_count}/{task.organizer.max_calls} "
            f"writer={task.writer.actual_call_count}/{task.writer.max_calls} "
            f"reviewer={task.reviewer.actual_call_count}/{task.reviewer.max_calls} "
            f"activation={task.activation_decision}"
        )
    return "\n".join(rows)


def _assisted_state_summary(state: object) -> str:
    rows: list[str] = []
    for task in state.tasks:  # type: ignore[attr-defined]
        rows.append(
            f"{task.task_id}: {task.status} "
            f"writer={task.writer.actual_call_count}/{task.writer.max_calls} "
            f"reviewer={task.reviewer.actual_call_count}/{task.reviewer.max_calls}"
        )
    return "\n".join(rows)
