"""SprintBaton CLI — the only UI for now.

Commands:
  sprintbaton setup                            interactive first run: credentials,
                                               provider, seeded agents, role bindings
  sprintbaton repo create -f <manifest.yaml>  onboard a repository (upsert by name)
  sprintbaton init --project "My Web App"     queue a metadata init run (runs under serve)
  sprintbaton serve                            polling job + orchestrator worker
  sprintbaton providers add|update|list|delete|set-limit
                                               register a model provider, seed its
                                               (runtime x model-tier) agents and bind
                                               the 13 task actions to them
                                               (provider-setup-cli spec)
  sprintbaton agents create|list|delete        manage AgentDefinitions (harness + model
                                               + optional system-preamble recipes)
  sprintbaton credentials create|list|delete   named provider tokens (repo-level overrides)
  sprintbaton reports [timeseries|by-task|by-agent|by-state]
                                               token usage reports (token-usage-reporting spec;
                                               no subcommand = by-task over the last 7 days)

There is also a hidden `harness-guard` command — the PreToolUse hook
entrypoint the claude_code_cli harness's generated settings.json shells into
(claude-code-cli harness spec §5.3). Never invoked by humans.
"""

import logging
import os
from pathlib import Path

import typer

app = typer.Typer(help="SprintBaton — an asynchronous coding agent that runs on your todolist.")
agents_app = typer.Typer(help="Manage AgentDefinitions — named harness+model recipes bound "
                              "onto task actions, by `providers add --bind-roles` or by a "
                              "SPRINTBATON_<ACTION>_AGENT env var.")
app.add_typer(agents_app, name="agents")
repo_app = typer.Typer(help="Onboard and manage projects — one todolist board mapped to N "
                            "git repositories (multi-repo-project spec).")
app.add_typer(repo_app, name="project")
app.add_typer(repo_app, name="repo")  # back-compat alias
credentials_app = typer.Typer(help="Manage named provider credentials — encrypted tokens a "
                                   "repository can reference as an override.")
app.add_typer(credentials_app, name="credentials")
providers_app = typer.Typer(help="Register a model provider — installs the selected runtimes' "
                                 "optional dependencies, captures a credential, seeds the "
                                 "(runtime x model-tier) AgentDefinitions, optionally binds "
                                 "every task action to them, and can carry self-imposed token "
                                 "budgets used for fallback routing.")
app.add_typer(providers_app, name="providers")
reports_app = typer.Typer(help="Token usage reports over TaskActionEvent — timeseries, "
                               "per-task, per-agent, per-task-state breakdowns "
                               "(token-usage-reporting spec).")
app.add_typer(reports_app, name="reports")
log = logging.getLogger(__name__)


def _report_error(message: str, *, cause: str = "", fix: str = "") -> None:
    """Print an error the way every CLI error is printed: what went wrong,
    what most likely caused it, and the command that puts it right. No error
    leaves this module without a `fix` — a message that only says what failed
    sends the user to the docs for something the CLI already knows."""
    typer.echo(f"error: {message}", err=True)
    if cause:
        typer.echo(f"  likely cause: {cause}", err=True)
    if fix:
        typer.echo(f"  to fix: {fix}", err=True)


def _fail(message: str, *, cause: str = "", fix: str = "", code: int = 1):
    """_report_error, then exit non-zero."""
    _report_error(message, cause=cause, fix=fix)
    raise typer.Exit(code=code)


def _startup(build):
    """Run a startup step (`build_container`, `create_app`), turning the two
    failures a user can cause and fix — a bad setting, an unreachable backend —
    into a CLI error. Anything else is a bug and keeps its traceback."""
    from sprintbaton.dependencies import MissingDependencyError

    try:
        return build()
    except MissingDependencyError:
        raise                              # reported by main()
    except ValueError as e:
        _fail(f"invalid configuration: {e}",
              cause="a setting in the environment or in ./.env has a value "
                    "SprintBaton does not support",
              fix="correct the variable named above (the README's "
                  "configuration reference lists every setting and its valid "
                  "values); a local install needs none of them — "
                  "`SPRINTBATON_MODE=tool` is the zero-infrastructure default")
    except Exception as e:
        kind = type(e).__name__
        unreachable = isinstance(e, (OSError, TimeoutError)) or any(
            word in kind for word in ("Timeout", "Connection", "Operational",
                                      "EndpointConnection", "ServerSelection"))
        if not unreachable:
            raise
        detail = (str(e).splitlines() or [kind])[0][:240]
        _fail(f"cannot reach a backing service ({kind}): {detail}",
              cause="this install is configured for a database, queue or "
                    "object store that is not running or not reachable — "
                    "hosted mode expects MongoDB, Redis and S3-compatible "
                    "storage by default",
              fix="start that service, or point MONGO_BASE_URI / POSTGRES_DSN "
                  "/ REDIS_URI / S3_ENDPOINT at the right address; for a "
                  "single-user local install set `SPRINTBATON_MODE=tool`, "
                  "which needs none of them")


def _container():
    from sprintbaton.container import build_container

    return _startup(build_container)


_LOCK_BUSY = dict(
    cause="another SprintBaton process (a `project create`, an `init`, or the "
          "worker publishing metadata) is holding this project's "
          "initialization lock",
    fix="wait a few seconds and run the same command again")


def _read_manifest(file: Path, example: str) -> str:
    """The manifest's text, or a clean error when the path cannot be read."""
    try:
        return file.read_text()
    except OSError as e:
        _fail(f"cannot read manifest {str(file)!r}: {e.strerror or e}",
              cause="the path is misspelled, is a directory, or is not "
                    "readable from the current directory",
              fix=f"check the path and re-run `{example}`")


def _summary(help_text: str | None) -> str:
    """The leading clause of a command's docstring — what it does, without
    the parentheticals and the qualifications that follow a dash or a colon."""
    import re

    first = " ".join((help_text or "").split("\n\n")[0].split())
    first = re.sub(r"\s*\([^)]*\)", "", first)
    for stop in (" — ", ": ", ". "):
        first = first.split(stop)[0]
    return first.rstrip(".:")


def _missing_command(ctx: typer.Context, helpful: tuple = ()) -> None:
    """A command (or command group) run with nothing after it is an error, and
    like every error it says what to run instead: `helpful` when given, else
    each of the group's own subcommands, and always the `--help` that lists
    them all. Exit status 2, as for any usage error."""
    if ctx.invoked_subcommand is not None or ctx.resilient_parsing:
        return
    # Not ctx.command_path: its first word is however the program was started
    # (`python -m sprintbaton.cli`, a test runner), never reliably the command
    # the user should type.
    names, node = [], ctx
    while node.parent is not None:
        names.append(node.info_name)
        node = node.parent
    path = " ".join(["sprintbaton", *reversed(names)])
    rows = list(helpful) or [
        (f"{path} {name}", _summary(command.help))
        for name, command in sorted(ctx.command.commands.items())
        if not command.hidden]
    rows.append((f"{path} --help", "list every command and option"))
    width = max(len(command) for command, _ in rows)
    _fail(f"missing command — `{path}` must be followed by a command",
          cause="it was run on its own, with nothing after it",
          fix="run one of these:\n" + "\n".join(
              f"    {command.ljust(width)}   {what}" for command, what in rows),
          code=2)


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context):
    _missing_command(ctx, helpful=(
        ("sprintbaton setup", "set SprintBaton up (credentials, model "
                              "provider, role bindings)"),))


def _group_requires_command(group: typer.Typer) -> None:
    @group.callback(invoke_without_command=True)
    def _bare(ctx: typer.Context):
        _missing_command(ctx)


for _group in (agents_app, repo_app, credentials_app, providers_app):
    _group_requires_command(_group)


def _configure_logging(process_role: str, *, verbose: bool = False,
                       quiet: bool = False, json_output: bool = False) -> None:
    """Resolve verbosity and pick the output surface before build_container's
    setup_logging runs (cli-logging spec §3, §8): flags > env > normal, and
    the console renderer is installed for tool-mode interactive runs — when it
    is, setup_logging skips its JSON handler."""
    from sprintbaton.config.settings import get_settings
    from sprintbaton.observer.console import install_console_handler
    from sprintbaton.observer.telemetry import set_process_role
    from sprintbaton.observer.verbosity import resolve_verbosity, set_verbosity

    if verbose and quiet:
        _fail("--verbose and --quiet are mutually exclusive",
              cause="both flags were passed in one invocation",
              fix="pass only one — `--verbose` to stream reasoning traces, or "
                  "`--quiet` for warnings and errors only")
    settings = get_settings()
    try:
        verbosity = resolve_verbosity(settings.sprintbaton_log_verbosity,
                                      verbose=verbose, quiet=quiet)
    except ValueError as e:
        _fail(str(e),
              cause="SPRINTBATON_LOG_VERBOSITY is set to an unsupported value in "
                    "the environment or in ./.env",
              fix="`export SPRINTBATON_LOG_VERBOSITY=normal` (or unset it), or "
                  "pass `--verbose` / `--quiet` for this one run")
    set_verbosity(verbosity, base_level=settings.log_level)
    set_process_role(process_role)
    install_console_handler(verbosity, mode=settings.sprintbaton_mode,
                            force_json=json_output)


def _upsert_env(key: str, value: str, env_path: Path = Path(".env")) -> None:
    """Write KEY=value into a .env file, replacing any existing line for the key.

    The one remaining caller is SPRINTBATON_DEFAULT_PROVIDER (provider-setup-cli
    spec §6.1): a single, non-load-bearing value recording which provider
    `setup` and `providers update` should default to. Agent *bindings* go to
    UserConfiguration instead — `.env` is resolved relative to the current
    working directory, which for 14 bindings would mean running the command
    from the wrong directory silently produces an unconfigured system (§2.6).
    """
    lines: list[str] = []
    found = False
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.strip().startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")


def _print_status(view, project) -> None:
    """Render initialization_status_view for a human (spec §10.2/§10.4)."""
    from datetime import datetime, timezone

    def when(millis):
        if not millis:
            return "-"
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC")

    typer.echo(f"project: {project.title} ({project.id})")
    if view.taskId is None:
        typer.echo("latest run: none")
    else:
        typer.echo(f"latest run: {view.taskId} — {view.status} "
                   f"(trigger={view.trigger}, scope={view.scope}, created {when(view.createdAt)})")
    if view.status == "retrying":
        typer.echo(f"  retrying at {when(view.retryAfter)} after "
                   f"{view.transientFailures} transient failure(s): {view.lastTransientError}")
    for entry in view.repos:
        detail = f" — {entry.error}" if entry.error else ""
        revision = f" revision={entry.revision}" if entry.revision else ""
        typer.echo(f"  - repo {entry.repoId}: {entry.status}{revision}"
                   f" (validation bounces {entry.validationRounds}, "
                   f"continuations {entry.continuationRounds}){detail}")
    if view.error:
        typer.echo(f"error: {view.error}")
    typer.echo(f"current revision: {view.metadataRevision or '(none)'}; "
               f"initialized: {when(view.metadataInitializedAt)}")
    gate = "open" if view.gateOpen else "CLOSED — board tasks wait in TaskPending"
    if view.gateOverridden:
        gate += " (overridden)"
    typer.echo(f"gate: {gate}")
    for remedy in view.remedies:
        typer.echo(f"  remedy: {remedy}")


@app.command()
def init(
    project: str = typer.Option(..., help="Project title (metadata.name in the manifest)"),
    missing_only: bool = typer.Option(False, "--missing-only",
                                      help="Only regenerate repos with no published metadata"),
    wait: bool = typer.Option(False, "--wait",
                              help="Block until the run finishes (needs a running "
                                   "`sprintbaton serve`)"),
    status: bool = typer.Option(False, "--status",
                                help="Show the latest run and gate state, then exit"),
    verbose: bool = typer.Option(False, "--verbose", "-v",
                                 help="Stream reasoning traces live"),
    quiet: bool = typer.Option(False, "--quiet", "-q",
                               help="Suppress all log output below warnings"),
    json_output: bool = typer.Option(False, "--json",
                                     help="Force structured JSON output even in tool mode"),
):
    """Queue a metadata initialization run for a project: per-repo `.sprintbaton/`
    metadata for every member, then the combined project index (project-
    initialization-task spec §10.2). The run is an invisible task that
    `sprintbaton serve` discovers and executes — this command never calls a
    model or clones anything itself."""
    import time

    _configure_logging("cli", verbose=verbose, quiet=quiet, json_output=json_output)
    from sprintbaton.entities.enums import InitializationTrigger, MetadataScope
    from sprintbaton.metadata.initialization import (
        InitializationInProgress,
        initialization_status_view,
        latest_initialization_task,
        request_initialization,
    )
    from sprintbaton.metadata.revisions import MetadataLockTimeout

    # `init` is per-project and stays that way: the old --default-provider flag
    # wrote one global value from a project-scoped command, so configuring one
    # project silently changed another's resolution (provider-setup-cli spec
    # §2.9/§4.7). Provider choice lives in `sprintbaton setup` / `providers add`.
    container = _container()
    ctx = container.ctx
    stale_after = container.settings.sprintbaton_reconcile_stale_after_seconds
    user_id = container.settings.sprintbaton_user_id
    proj = ctx.project_repo.find_one({"title": project, "userId": user_id})
    if proj is None:
        _fail(f"unknown project: {project}",
              cause="the project was never onboarded, or the title is "
                    "misspelled (titles are matched exactly)",
              fix="`sprintbaton project list` shows the onboarded titles; "
                  "onboard a new one with `sprintbaton project create -f "
                  "<manifest.yaml>`")

    def view():
        current = ctx.project_repo.get(proj.id) or proj
        return initialization_status_view(
            current, latest_initialization_task(ctx.task_repo, current),
            stale_after_seconds=stale_after), current

    if status:
        _print_status(*view())
        return

    if not any(r.remoteUrl for r in ctx.repositories_for(proj)):
        _fail(f"project {proj.title!r} has no repository with a remoteUrl to clone",
              cause="no entry under `repositories` in the project manifest "
                    "sets `remoteUrl`, so there is nothing to generate "
                    "metadata from",
              fix="add `remoteUrl: <git url>` to a repository in the manifest "
                  "and re-apply it with `sprintbaton project create -f "
                  "<manifest.yaml>`")
    scope = MetadataScope.Missing if missing_only else MetadataScope.All
    try:
        task = request_initialization(proj, task_repo=ctx.task_repo, lock=container.lock,
                                      scope=scope, trigger=InitializationTrigger.CliInit)
    except InitializationInProgress as e:
        _fail(f"an initialization run is already in progress for {proj.title}: "
              f"task {e.task.id}",
              cause="an earlier `sprintbaton init` (or the automatic run "
                    "`project create` queues) has not finished — it only "
                    "advances while `sprintbaton serve` is running",
              fix=f"`sprintbaton init --project \"{proj.title}\" --status` shows "
                  f"its progress; start `sprintbaton serve` if it is not "
                  f"running, and re-run this once the run completes")
    except MetadataLockTimeout as e:
        _fail(str(e), **_LOCK_BUSY)
    typer.echo(f"metadata initialization queued for {proj.title} (task {task.id}, "
               f"scope={scope.value}) — runs under `sprintbaton serve`")
    if not wait:
        return

    typer.echo("waiting for the run to finish — `sprintbaton serve` must be running "
               "to pick it up (Ctrl+C stops waiting, not the run)")
    last = None
    while True:
        current_view, current = view()
        if current_view.taskId != task.id:
            # A newer run superseded the one we queued — report on ours.
            current_view = initialization_status_view(
                current, ctx.task_repo.get(task.id), stale_after_seconds=stale_after)
        if current_view.status != last:
            typer.echo(f"  status: {current_view.status}")
            last = current_view.status
        if current_view.status in ("completed", "failed"):
            _print_status(current_view, current)
            raise typer.Exit(code=0 if current_view.status == "completed" else 1)
        time.sleep(5)


@app.command()
def serve(
    verbose: bool = typer.Option(False, "--verbose", "-v",
                                 help="Stream reasoning traces live"),
    quiet: bool = typer.Option(False, "--quiet", "-q",
                               help="Suppress all log output below warnings"),
    json_output: bool = typer.Option(False, "--json",
                                     help="Force structured JSON output even in tool mode"),
):
    """Start the polling job and the orchestrator worker loop."""
    _configure_logging("worker", verbose=verbose, quiet=quiet, json_output=json_output)
    from sprintbaton.config.settings import get_settings
    from sprintbaton.observer.banner import show_banner
    from sprintbaton.orchestrator.recovery import reconcile_orphaned_tasks
    from sprintbaton.sandbox.base import SandboxUnavailableError

    if not (quiet or json_output) and get_settings().sprintbaton_mode == "tool":
        show_banner()
    container = _container()
    # Fail closed (hosted-sandbox-isolation spec §5.2, invariant 8): a worker
    # whose runs must be isolated refuses to start without a reachable sandbox
    # — it never falls back to running tenant code in its own process.
    runtime = container.sandbox
    if runtime.isolated:
        try:
            runtime.sandbox.health()
        except SandboxUnavailableError as e:
            _fail(f"refusing to start: {e}",
                  cause="hosted mode runs every task inside the sandbox "
                        "service and never falls back to this process; the "
                        "sandbox is down, or SPRINTBATON_SANDBOX_URL / "
                        "SPRINTBATON_SANDBOX_TOKEN do not match it",
                  fix="start the sandbox (`python -m sprintbaton.sandbox.server`) "
                      "and point SPRINTBATON_SANDBOX_URL and "
                      "SPRINTBATON_SANDBOX_TOKEN at it; for a single-user "
                      "local install run `SPRINTBATON_MODE=tool sprintbaton "
                      "serve` instead")
        if runtime.broker is not None:
            runtime.broker.start()
    # Surface an install that never ran `providers add --bind-roles` once, here,
    # with the remedy — instead of as a resolution error on the first task that
    # happens to reach that action (provider-setup-cli spec §7.3).
    _warn_unbound_actions(container)
    # A bound agent whose harness package is missing from this install fails
    # here, not on the first task that reaches it (pluggable-hosted-backends
    # spec §4.9). Hosted refuses; tool mode warns.
    if not _check_bound_dependencies(container):
        raise typer.Exit(code=1)
    # Crash recovery before intake starts (zero-infra-storage spec §4.3): the
    # queue is a non-durable hint, so re-derive "still needs the orchestrator"
    # from the entity store's stale in-flight claims.
    requeued = reconcile_orphaned_tasks(
        container.ctx.task_repo, container.task_queue,
        container.settings.sprintbaton_reconcile_stale_after_seconds)
    if requeued:
        typer.echo(f"crash recovery: re-enqueued {len(requeued)} orphaned task(s)")
    # Surface failed first initializations to users who never read logs
    # (project-initialization-task spec §5.7 channel 4).
    from sprintbaton.metadata.initialization import failed_closed_gates
    for failed_project, failed_run in failed_closed_gates(
            container.ctx.project_repo, container.ctx.task_repo):
        typer.echo(f"metadata initialization FAILED for {failed_project.title} "
                   f"(task {failed_run.id}): {failed_run.initializationError} — "
                   f"board tasks wait until it succeeds; see `sprintbaton init "
                   f"--project \"{failed_project.title}\" --status`")
    container.polling_job.start()
    container.release_job.start()
    container.usage_limit_wake_job.start()
    container.workspace_sweep_job.start()
    typer.echo("SprintBaton serving — polling + release-window + usage-limit-wake "
               "+ workspace-sweep jobs started, worker loop running. Ctrl+C to stop.")

    try:
        while True:
            task_id = container.task_queue.dequeue_task(timeout_seconds=5)
            if task_id is None:
                continue
            try:
                container.orchestrator.process(task_id)
            except Exception:
                log.exception("task processing failed",
                              extra={"event": "error", "task_id": task_id})
    except KeyboardInterrupt:
        container.polling_job.stop()
        container.release_job.stop()
        container.usage_limit_wake_job.stop()
        container.workspace_sweep_job.stop()
        typer.echo("stopped.")


# --------------------------------------------------------------------- setup

_CREDENTIAL_PROMPTS = (
    ("github", "GitHub token (repo scope — clones, pushes and opens PRs)"),
    ("todoist", "Todoist API token (the board SprintBaton reads tasks from)"),
)


def _stdin_is_tty() -> bool:
    """Whether stdin is a terminal. A named function rather than an inline
    `sys.stdin.isatty()` so tests can exercise `setup`'s interactive path —
    click's CliRunner substitutes a non-tty stdin, which otherwise makes the
    prompting half of this command unreachable from a test."""
    import sys

    return sys.stdin.isatty()


@app.command()
def setup():
    """Interactive first-run configuration: credentials, model provider,
    seeded agents, role bindings. After this, go straight to `project create`
    and `init`.

    This command is a **wrapper**, never a second implementation
    (provider-setup-cli spec §8): it calls exactly what `credentials create`
    and `providers add --bind-roles` call, and owns only prompting and
    sequencing. Everything it does is reachable without a TTY through those
    commands, so CI and scripted installs never need it — which is why it
    refuses rather than prompting when stdin is not a terminal.
    """
    import shutil

    _configure_logging("cli", quiet=True)
    from sprintbaton.entities.enums import CredentialProvider
    from sprintbaton.observer.banner import BATON, TEXT, show_banner, style
    from sprintbaton.providers.seeding import (
        AGENTIC_RUNTIMES,
        CLASSIFY_RUNTIMES,
        PROVIDER_RUNTIME_HARNESS,
        SeedingError,
        default_runtimes,
        resolve_runtimes,
    )

    if not _stdin_is_tty():
        _fail("sprintbaton setup needs a terminal",
              cause="stdin is not interactive (a pipe, a script or CI), so "
                    "there is nobody to answer its prompts",
              fix="run it from a terminal, or run the same steps "
                  "non-interactively:\n"
                  "    sprintbaton credentials create --provider github  --token ...\n"
                  "    sprintbaton credentials create --provider todoist --token ...\n"
                  "    sprintbaton providers add <provider> --bind-roles")

    container = _container()
    user_id = container.settings.sprintbaton_user_id
    show_banner(animate=not container.settings.sprintbaton_no_animation)
    typer.echo(
        f"\n{style('Welcome to SprintBaton', bold=True)}"
        f"{style(' — an asynchronous coding agent that runs on your todolist.', TEXT)}\n\n"
        "This setup will:\n"
        "  1. store your GitHub and Todolist tokens\n"
        "  2. pick a model provider and its runtimes\n"
        "  3. seed agents and bind them to every role\n\n"
        + style("Ctrl+C to stop; nothing is written until each step is answered.",
                BATON[1]) + "\n")

    # 1. Credentials -------------------------------------------------------
    stored = {str(c.provider) for c in container.ctx.credentials.list_for(user_id)}
    for provider_value, prompt in _CREDENTIAL_PROMPTS:
        if provider_value in stored:
            typer.echo(f"{provider_value}: credential already stored — skipping")
            continue
        token = typer.prompt(prompt, default="", hide_input=True,
                             show_default=False)
        if not token.strip():
            typer.echo(f"  skipped {provider_value} — store it later with "
                       f"`sprintbaton credentials create --provider "
                       f"{provider_value} --token ...`")
            continue
        credential = container.ctx.credentials.store(
            user_id, CredentialProvider(provider_value), token.strip(),
            is_default=None)
        typer.echo(f"  stored {provider_value} {credential.maskedKey}")

    # 2. Provider ----------------------------------------------------------
    # CLI-binary detection is advisory, matching `providers add`'s posture: a
    # missing binary is reported, never a block (§8).
    typer.echo("\nModel provider:")
    choices = sorted(PROVIDER_RUNTIME_HARNESS)
    binaries = {"anthropic": "claude", "openai": "codex", "google": "gemini"}
    for choice in choices:
        binary = binaries.get(choice)
        if binary and shutil.which(binary):
            status = f"  (`{binary}` found on PATH)"
        elif binary:
            status = f"  (`{binary}` not on PATH — the cli runtime needs it)"
        else:
            status = ""
        typer.echo(f"  - {choice}{status}")
    current = container.settings.sprintbaton_default_provider
    provider = typer.prompt(
        "provider", default=current if current in choices else "anthropic")
    if provider not in choices:
        _fail(f"unknown provider {provider!r}",
              cause=f"setup can seed only {', '.join(choices)}",
              fix="run `sprintbaton setup` again and enter one of those "
                  "names (credentials already stored are kept and skipped)")

    # 3. Runtimes ----------------------------------------------------------
    settings = container.user_service.settings_for(user_id)
    d_classify, d_agentic = default_runtimes(settings, provider)
    typer.echo(f"\nRuntimes (this install's defaults for {provider}: "
               f"classify={d_classify}, agentic={d_agentic}).")
    classify = typer.prompt(
        f"  classify runtime [{'/'.join(CLASSIFY_RUNTIMES)}]", default="default")
    agentic = typer.prompt(
        f"  agentic runtime [{'/'.join(AGENTIC_RUNTIMES)}]", default="default")

    # An API key is not merely unneeded on the `cli` runtime — it is
    # unreachable, because a CLI harness never forwards one (cli-subscription-
    # auth-parity spec §4.7). Offering it there invites exactly the ambient-key
    # shadowing this design removed, so resolve the two runtime answers and
    # only ask when some seeded row could actually use the key.
    try:
        r_classify, r_agentic = resolve_runtimes(settings, classify, agentic)
    except SeedingError as exc:
        # A typo'd runtime would otherwise surface as a traceback here rather
        # than as _provider_setup's clean message further down.
        _fail(str(exc),
              cause="a runtime answer was not one of the listed choices, or "
                    "this provider does not offer that runtime",
              fix="run `sprintbaton setup` again and press Enter at both "
                  "runtime prompts to accept `default`")
    token = ""
    if r_classify == "cli" and r_agentic == "cli":
        binary = binaries.get(provider, provider)
        typer.echo(f"\nEvery role will run on the {provider} CLI, which "
                   f"authenticates only from its own login session — "
                   f"SprintBaton never forwards an API key to it. Make sure "
                   f"`{binary}` is logged in before `sprintbaton serve`.")
        typer.echo(f"  (If you later bind an agent-SDK or API row, store a key "
                   f"with `sprintbaton credentials create --provider "
                   f"{provider} --token ...`.)")
    else:
        cli_half = ("classify" if r_classify == "cli"
                    else "agentic" if r_agentic == "cli" else "")
        scope = (f" It covers the {'agentic' if cli_half == 'classify' else 'classify'} "
                 f"roles only — the {cli_half} roles run on the {provider} CLI "
                 f"login and never receive a key." if cli_half else "")
        if typer.confirm(f"\nStore a {provider} API key now?{scope}",
                         default=False):
            token = typer.prompt("  API key", hide_input=True).strip()
        # The agent-SDK runtime spawns the `claude` binary, which can run on
        # the user's own Claude subscription instead of a metered key — a
        # `claude setup-token` token stored as its own credential kind
        # (hosted-sandbox-isolation spec §9.1). Preferred when present (§9.2).
        if (provider == "anthropic" and r_agentic == "agent_sdk"
                and "anthropic_subscription" not in stored
                and typer.confirm("\nStore a Claude subscription token "
                                  "(`claude setup-token`) for the agentic roles?",
                                  default=False)):
            sub_token = typer.prompt("  subscription token", hide_input=True).strip()
            if sub_token:
                credential = container.ctx.credentials.store(
                    user_id, CredentialProvider.ANTHROPIC_SUBSCRIPTION, sub_token)
                typer.echo(f"  stored anthropic_subscription {credential.maskedKey}")

    # 4. Seed + bind — the same call `providers add --bind-roles` makes -----
    _provider_setup(container, name=provider, file=None, token=token or None,
                    classify_harness=classify, agentic_harness=agentic,
                    seed=True, bind=True, skip_install=False,
                    update_existing=False)

    typer.echo("\nNext:\n"
               "  sprintbaton project create -f <manifest.yaml>\n"
               "  sprintbaton init --project \"<title>\"\n"
               "  sprintbaton serve")


def _warn_unbound_actions(container) -> None:
    """Report task actions with no AgentDefinition bound, for the process's own
    user. In hosted mode each tenant binds their own, so this is a check on the
    deployment-wide defaults rather than an exhaustive one."""
    from sprintbaton.models.definitions import BINDING_REMEDY
    from sprintbaton.providers.seeding import unbound_actions

    user_id = container.settings.sprintbaton_user_id
    missing = unbound_actions(container.user_service.settings_for(user_id))
    if not missing:
        return
    typer.echo(f"WARNING: no agent bindings found for {len(missing)} task "
               f"action(s): {', '.join(missing)}")
    typer.echo(f"  Tasks reaching them will be parked as Blocked. To fix: "
               f"{BINDING_REMEDY}")


def _check_bound_dependencies(container) -> bool:
    """Report bound agents whose harness dependencies are not installed.
    Returns False when `serve` must refuse to start: hosted mode with a chain
    that has no installable entry left. A chain with a usable fallback entry,
    and anything in tool mode, only warns."""
    from sprintbaton.providers.dependency_check import (
        check_bound_dependencies,
        describe,
    )

    settings = container.settings
    user_id = settings.sprintbaton_user_id
    hosted = settings.sprintbaton_mode == "hosted"
    reports = check_bound_dependencies(
        container.user_service.settings_for(user_id), container.agent_definitions,
        user_id)
    fatal = [r for r in reports if hosted and not r.satisfiable]
    for report in reports:
        prefix = "ERROR" if report in fatal else "WARNING"
        for line in describe(report, hosted):
            typer.echo(f"{prefix}: {line}" if not line.startswith("  ") else line,
                       err=bool(fatal))
    if fatal:
        _report_error(
            "refusing to start: the image is missing packages its agent "
            "bindings need",
            cause="an agent bound to a task action uses a harness whose pip "
                  "extra was not built into this image",
            fix="rebuild the image with the extras named above in its "
                "PROVIDER_EXTRAS build argument, or rebind the roles to a "
                "provider the image carries with `sprintbaton providers "
                "update <provider> --bind-roles`")
    return not fatal


@app.command("serve-api")
def serve_api(
    host: str = typer.Option(None, help="Bind address (default: SPRINTBATON_API_HOST)"),
    port: int = typer.Option(None, help="Port (default: SPRINTBATON_API_PORT)"),
):
    """Start the hosted-mode CRUD API (user-multitenancy spec §9) — a second
    front door onto the same storage `sprintbaton serve` acts on. The two are
    independently scalable deployables; CLI/local installs never need this.
    Requires the `api` extra (`pip install sprintbaton[api]`, included in
    [hosted]) — fastapi/uvicorn/bcrypt live there, not in the base CLI install."""
    from sprintbaton.dependencies import require_storage_module

    uvicorn = require_storage_module("uvicorn", package="uvicorn", extra="api",
                                     feature="`sprintbaton serve-api`")
    require_storage_module("fastapi", package="fastapi", extra="api",
                           feature="`sprintbaton serve-api`")

    from sprintbaton.api.app import create_app
    from sprintbaton.config.settings import get_settings
    from sprintbaton.observer.telemetry import setup_logging

    # Hosted deployable: JSON logs unconditionally, verbosity from the env
    # only — no console renderer and no reasoning traces to stream here
    # (cli-logging spec §9); process_role="api" distinguishes its lines from
    # the worker's, which shares the same OTEL_SERVICE_NAME (§4.3).
    _configure_logging("api", json_output=True)
    settings = get_settings()
    setup_logging(settings)
    uvicorn.run(
        _startup(lambda: create_app(settings)),
        host=host or settings.sprintbaton_api_host,
        port=port or settings.sprintbaton_api_port,
    )


# ------------------------------------------------------------------ project

def _confirm_column_plan(spec, plan_error):
    """Render a column-provisioning plan and block on interactive confirmation
    (todoist-label-routing spec §5). Returns the spec with confirmColumnProvisioning
    set so the retry actually creates the sections. Aborts on a declined prompt."""
    matched = [p for p in plan_error.plan if p.action in ("explicit", "matched")]
    to_create = [p for p in plan_error.plan if p.action == "create"]
    typer.echo("Column mapping plan for this Todoist board:")
    for p in matched:
        typer.echo(f"  ✓ {p.columnKey}: uses existing section "
                   f"{p.sectionName!r} ({p.sectionId})")
    typer.echo("  The following sections will be CREATED on your live board:")
    for p in to_create:
        typer.echo(f"  + {p.columnKey}: create section {p.sectionName!r}")
    if not typer.confirm("Create these sections?", default=False):
        _fail("aborted — no sections created and the project was not saved",
              cause="you declined to create the missing board sections, and "
                    "a project needs every column mapped",
              fix="re-run `sprintbaton project create -f <manifest.yaml>` and "
                  "confirm, or map every column to an existing section id "
                  "under `spec.columns` in the manifest")
    return spec.model_copy(update={"confirmColumnProvisioning": True})


@repo_app.command("create")
def repo_create(
    file: Path = typer.Option(..., "--file", "-f", help="Project manifest (k8s-style YAML)"),
):
    """Apply a Project manifest — an upsert keyed on metadata.name (and its
    member repositories by name within it), so re-applying the same file is
    idempotent (multi-repo-project spec §11). When the manifest maps only some
    (or none) of the board columns, the missing ones are matched against the
    live Todoist board and the rest proposed for creation — shown for
    confirmation before anything is created (todoist-label-routing spec §3)."""
    from sprintbaton.adaptors.base import BoardProvisioningError, ProvisioningNotSupported
    from sprintbaton.onboarding import (
        ColumnProvisioningRequired,
        OnboardingError,
        apply_project,
        parse_project_manifest,
        provision_provider_routing,
    )

    import yaml

    from sprintbaton.metadata.revisions import MetadataLockTimeout

    container = _container()

    def _apply(spec):
        return apply_project(
            spec,
            user_id=container.settings.sprintbaton_user_id,
            project_repo=container.ctx.project_repo,
            repo_repo=container.ctx.repo_repo,
            credentials=container.ctx.credentials,
            settings=container.settings,
            task_repo=container.ctx.task_repo,
            lock=container.lock,
        )

    text = _read_manifest(file, "sprintbaton project create -f <manifest.yaml>")
    rerun = f"`sprintbaton project create -f {file}`"
    try:
        spec = parse_project_manifest(text)
        try:
            result = _apply(spec)
        except ColumnProvisioningRequired as plan_error:
            spec = _confirm_column_plan(spec, plan_error)
            result = _apply(spec)
    except yaml.YAMLError as e:
        _fail(f"{file} is not valid YAML: {e}",
              cause="a syntax error in the manifest — usually indentation or "
                    "an unquoted special character",
              fix=f"correct the line named above and re-run {rerun}")
    except OnboardingError as e:
        _fail(str(e),
              cause="the manifest is missing a required field, has a wrong "
                    "value, or references a credential id you do not own",
              fix=f"correct the manifest (the README's Quick start has a "
                  f"complete `kind: Project` example; `sprintbaton credentials "
                  f"list` shows valid credential ids) and re-run {rerun}")
    except ProvisioningNotSupported as e:
        _fail(str(e),
              cause="this todolist provider cannot create or look up board "
                    "columns for you",
              fix=f"map every column to an existing section id under "
                  f"`spec.columns` in the manifest and re-run {rerun}")
    except BoardProvisioningError as e:
        _fail(str(e),
              cause="the todolist API rejected a request part-way through — "
                    "most often a missing or expired token; nothing is rolled "
                    "back, so the board named above may be left half-built",
              fix=f"store a working token with `sprintbaton credentials create "
                  f"--provider todoist --token ...`, delete the partial board "
                  f"in the todolist app, and re-run {rerun}")
    except MetadataLockTimeout as e:
        _fail(str(e), **_LOCK_BUSY)
    project, created = result.project, result.created

    # Establish the provider's routing mechanism once at link time (§2.3) —
    # for Todoist, the sprintbaton-agent/-human labels. Best-effort.
    provision_provider_routing(project, credentials=container.ctx.credentials,
                               settings=container.settings)
    verb = "created" if created else "updated"
    members = container.ctx.repositories_for(project)
    typer.echo(f"{verb} {project.id}: {project.title} (board {project.boardId}, "
               f"provider {project.todolistProvider}, {len(members)} repo(s), "
               f"active={project.active})")
    if result.initialization_task is not None:
        typer.echo(f"metadata generation queued (task {result.initialization_task.id}) "
                   f"— runs under 'sprintbaton serve'")
    for hint in result.hints:
        typer.echo(hint)


@repo_app.command("metadata-gate")
def repo_metadata_gate(
    name: str = typer.Argument(..., help="Project title (metadata.name)"),
    open_gate: bool = typer.Option(None, "--open/--enforce",
                                   help="--open lets board tasks run without metadata "
                                        "for now; --enforce restores the gate"),
):
    """Override the metadata gate (project-initialization-task spec §5.4): with
    --open, board tasks run before the project's first successful metadata
    initialization — generation keeps running and retrying, and its first
    success clears the override. Not the same as `generateMetadata: false`."""
    from sprintbaton.metadata.initialization import (
        initialization_status_view,
        latest_initialization_task,
        set_metadata_gate_override,
    )
    from sprintbaton.metadata.revisions import MetadataLockTimeout

    if open_gate is None:
        _fail("pass --open or --enforce",
              cause="the command was run without saying which way to set the gate",
              fix=f"`sprintbaton project metadata-gate \"{name}\" --open` lets "
                  f"board tasks run before metadata exists; `--enforce` makes "
                  f"them wait for it again")
    container = _container()
    user_id = container.settings.sprintbaton_user_id
    project = container.ctx.project_repo.find_one({"title": name, "userId": user_id})
    if project is None:
        _fail(f"no project named {name!r}",
              cause="the project was never onboarded, was deleted, or the "
                    "title is misspelled (titles are matched exactly)",
              fix="`sprintbaton project list` shows the onboarded titles")
    try:
        project = set_metadata_gate_override(
            project, open_gate, project_repo=container.ctx.project_repo,
            lock=container.lock, user_id=user_id)
    except MetadataLockTimeout as e:
        _fail(str(e), **_LOCK_BUSY)
    _print_status(initialization_status_view(
        project, latest_initialization_task(container.ctx.task_repo, project),
        stale_after_seconds=container.settings.sprintbaton_reconcile_stale_after_seconds),
        project)


@repo_app.command("list")
def repo_list():
    """List every onboarded project and its member repositories."""

    container = _container()
    projects = container.ctx.project_repo.find(
        {"userId": container.settings.sprintbaton_user_id})
    if not projects:
        typer.echo("no projects yet — onboard one with `sprintbaton project create -f`")
        return
    for p in projects:
        members = container.ctx.repositories_for(p)
        typer.echo(f"{p.title}: board={p.boardId} provider={p.todolistProvider} "
                   f"cadence={p.releaseCadenceDays}d active={p.active} id={p.id}")
        for r in members:
            typer.echo(f"    - {r.title} ({r.role or 'unspecified'}): "
                       f"github={r.githubRepo or '(none)'} id={r.id}")


@repo_app.command("delete")
def repo_delete(name: str = typer.Argument(..., help="Project title (metadata.name)")):
    """Soft-delete a project by name — the hard stop that also hides it
    (pausing intake only is `active: false` in the manifest)."""

    container = _container()
    project = container.ctx.project_repo.find_one(
        {"title": name, "userId": container.settings.sprintbaton_user_id})
    if project is None:
        _fail(f"no project named {name!r}",
              cause="the project was never onboarded, was deleted, or the "
                    "title is misspelled (titles are matched exactly)",
              fix="`sprintbaton project list` shows the onboarded titles")
    container.ctx.project_repo.soft_delete(project.id)
    typer.echo(f"deleted {name} ({project.id})")


# ------------------------------------------------------------ credentials

@credentials_app.command("create")
def credentials_create(
    provider: str = typer.Option(..., help="Provider: github, todoist, linear, anthropic, "
                                               "anthropic_subscription (a `claude setup-token` "
                                               "token), openai, gemini, open_hands_llm"),
    token: str = typer.Option(..., help="The plaintext token (encrypted at rest)"),
    no_default: bool = typer.Option(False, "--no-default",
                                    help="Create as an override-only credential, "
                                         "addressable by id from a repo manifest"),
):
    """Store an encrypted provider token. The first credential of a provider
    becomes your default automatically (repository-onboarding spec §4.1)."""
    from sprintbaton.entities.enums import CredentialProvider

    container = _container()
    try:
        provider_enum = CredentialProvider(provider)
    except ValueError:
        known = ", ".join(p.value for p in CredentialProvider)
        _fail(f"unknown provider: {provider}",
              cause=f"--provider must be one of: {known}",
              fix="re-run with one of those, e.g. `sprintbaton credentials "
                  "create --provider github --token ...`")
    credential = container.ctx.credentials.store(
        container.settings.sprintbaton_user_id, provider_enum, token,
        is_default=False if no_default else None,
    )
    typer.echo(f"created {credential.id}: {provider} {credential.maskedKey} "
               f"default={credential.isDefault}")


@credentials_app.command("list")
def credentials_list():
    """List stored credentials (masked — plaintext is never shown)."""

    container = _container()
    credentials = container.ctx.credentials.list_for(
        container.settings.sprintbaton_user_id)
    if not credentials:
        typer.echo("no credentials yet — create one with `sprintbaton credentials create`")
        return
    for c in credentials:
        view = container.ctx.credentials.public_view(c)
        typer.echo(f"{view['id']}: {view['provider']} {view['maskedKey']} "
                   f"default={view['isDefault']}")


@credentials_app.command("delete")
def credentials_delete(credential_id: str = typer.Argument(..., help="Credential id")):
    """Revoke a credential by id."""

    container = _container()
    if not container.ctx.credentials.delete(
            container.settings.sprintbaton_user_id, credential_id):
        _fail(f"no credential {credential_id!r}",
              cause="the id is mistyped, or the credential was already deleted "
                    "— this command takes the id, not the provider name",
              fix="`sprintbaton credentials list` shows each credential's id")
    typer.echo(f"deleted {credential_id}")


# ------------------------------------------------------------------ providers

# The two runtime flags share one implementation with `providers update` and
# `sprintbaton setup` (provider-setup-cli spec §8): there is exactly one code
# path that installs, registers, seeds and binds — the commands only differ in
# which of those steps they are allowed to mutate.

_CLASSIFY_HELP = ("Runtime for the four router-tier roles (classification, "
                  "spec/plan classification, repo scoping): default | api | "
                  "cli | agent_sdk. `default` = cli in tool mode, api in hosted.")
_AGENTIC_HELP = ("Runtime for the other nine roles: default | cli | agent_sdk. "
                 "`default` = cli in tool mode, agent_sdk in hosted. There is "
                 "deliberately no `api` — SprintBaton has no agentic-API "
                 "harness beyond anthropic's legacy raw_tool_loop.")


@providers_app.command("add")
def providers_add(
    name: str = typer.Argument(..., help="Provider: anthropic, openai, google, "
                                        "open_hands_llm"),
    file: Path = typer.Option(None, "--file", "-f",
                              help="Provider manifest (k8s-style YAML) — omit for flags"),
    token: str = typer.Option(None, help="API key to store (omit to rely on the CLI's own login)"),
    classify_harness: str = typer.Option("default", "--classify-harness",
                                         help=_CLASSIFY_HELP),
    agentic_harness: str = typer.Option("default", "--agentic-harness",
                                        help=_AGENTIC_HELP),
    seed_agents: bool = typer.Option(True, "--seed-agents/--no-seed-agents",
                                     help="Seed the (runtime x model-tier) "
                                          "AgentDefinition rows for this provider"),
    bind_roles: bool = typer.Option(False, "--bind-roles",
                                    help="Also bind all 13 task actions + the "
                                         "execution tier map to the seeded rows. "
                                         "Required at least once — nothing "
                                         "resolves without a binding."),
    skip_install: bool = typer.Option(False, "--skip-install",
                                      help="Register without attempting a pip install"),
    base_url: str = typer.Option(None, "--base-url",
                                 help="An https:// API endpoint other than the "
                                      "provider's default, e.g. an Anthropic-"
                                      "compatible gateway (hosted-sandbox-"
                                      "isolation spec §9.3)"),
):
    """Register a model provider, create-if-absent throughout (§4.6): install
    the selected runtimes' optional dependencies, warn if a CLI binary is
    missing, optionally capture a credential, persist the row, and seed four
    AgentDefinitions — one per (runtime, model tier). Re-running with the same
    arguments is a no-op. `providers update` is the deliberate mutation."""

    _provider_setup(
        _container(), name=name, file=file, token=token,
        classify_harness=classify_harness, agentic_harness=agentic_harness,
        seed=seed_agents, bind=bind_roles, skip_install=skip_install,
        update_existing=False, base_url=base_url)


@providers_app.command("update")
def providers_update(
    name: str = typer.Argument(..., help="Registered provider name"),
    file: Path = typer.Option(None, "--file", "-f", help="Provider manifest"),
    token: str = typer.Option(None, help="Replace the stored API key"),
    classify_harness: str = typer.Option("default", "--classify-harness",
                                         help=_CLASSIFY_HELP),
    agentic_harness: str = typer.Option("default", "--agentic-harness",
                                        help=_AGENTIC_HELP),
    bind_roles: bool = typer.Option(False, "--bind-roles",
                                    help="Bind the 13 roles + tier map to this "
                                         "provider's existing rows, changing no "
                                         "row contents."),
    skip_install: bool = typer.Option(False, "--skip-install",
                                      help="Do not attempt a pip install"),
    base_url: str = typer.Option(None, "--base-url",
                                 help="Set the provider's https:// endpoint "
                                      "(omit to keep the current one)"),
):
    """Change an existing provider's seeded agents or its role bindings — the
    deliberate mutation `providers add` refuses to be.

    `--bind-roles` switches which job this does, rather than adding a second
    one (§4.6) — one axis of change per invocation, so a behavior change is
    always attributable:

      providers update openai                 re-seed row contents from current
                                              Settings; bindings untouched
      providers update openai --bind-roles     bind the roles to the existing
                                              rows; row contents untouched
    """

    container = _container()
    if bind_roles:
        _provider_setup(container, name=name, file=file, token=None,
                        classify_harness=classify_harness,
                        agentic_harness=agentic_harness, seed=False, bind=True,
                        skip_install=True, update_existing=False,
                        require_registered=True)
        return
    _provider_setup(container, name=name, file=file, token=token,
                    classify_harness=classify_harness,
                    agentic_harness=agentic_harness, seed=True, bind=False,
                    skip_install=skip_install, update_existing=True,
                    require_registered=True, base_url=base_url)


def _provider_setup(container, *, name: str, file: Path | None, token: str | None,
                    classify_harness: str, agentic_harness: str, seed: bool,
                    bind: bool, skip_install: bool, update_existing: bool,
                    require_registered: bool = False,
                    base_url: str | None = None) -> None:
    """The one implementation behind `providers add`, `providers update` and
    `sprintbaton setup`. Owns no prompting and no policy of its own beyond the
    add-vs-update distinction its two boolean axes express."""
    import shutil

    from sprintbaton.providers.registry import builtin
    from sprintbaton.providers.seeding import (
        PROVIDER_RUNTIME_HARNESS,
        SeedingError,
        UNSEEDABLE_PROVIDERS,
        exec_rows_present,
        harnesses_for_rows,
        plan_rows,
        resolve_runtimes,
        seed_agents as write_rows,
    )
    from sprintbaton.providers.service import (
        ProviderCredentialSpec,
        ProviderError,
        ProviderSpec,
        parse_provider_manifest,
    )

    user_id = container.settings.sprintbaton_user_id
    settings = container.user_service.settings_for(user_id)
    try:
        if file is not None:
            spec = parse_provider_manifest(_read_manifest(
                file, "sprintbaton providers add -f <manifest.yaml>"))
        else:
            spec = ProviderSpec(
                name=name,
                credential=ProviderCredentialSpec(token=token) if token else None,
                baseUrl=base_url,
            )
    except (ProviderError, ValueError) as e:
        _fail(str(e),
              cause="the provider manifest or the command's arguments are "
                    "invalid — a missing name, an unsupported field, or a "
                    "--base-url that is not https",
              fix="correct it and re-run; the plain form is `sprintbaton "
                  "providers add <anthropic|openai|google> --bind-roles` "
                  "(`sprintbaton providers add --help` lists every option)")

    if require_registered and container.provider_service.get(user_id, spec.name) is None:
        _fail(f"provider {spec.name!r} is not registered",
              cause="`providers update` changes an existing provider, and this "
                    "one was never added (or its name is misspelled)",
              fix=f"`sprintbaton providers add {spec.name} --bind-roles` "
                  f"registers and binds it; `sprintbaton providers list` shows "
                  f"what is registered")

    b = builtin(spec.name)
    try:
        classify_runtime, agentic_runtime = resolve_runtimes(
            settings, classify_harness, agentic_harness, provider=spec.name)
        rows = plan_rows(spec.name, settings, classify_runtime=classify_runtime,
                         agentic_runtime=agentic_runtime)
    except SeedingError as e:
        if spec.name not in PROVIDER_RUNTIME_HARNESS and \
                spec.name not in UNSEEDABLE_PROVIDERS:
            seedable = ", ".join(sorted(PROVIDER_RUNTIME_HARNESS))
            _fail(str(e),
                  cause="the provider name is misspelled, or is not one "
                        "SprintBaton ships agents for",
                  fix=f"re-run with one of {seedable}, e.g. `sprintbaton "
                      f"providers add anthropic --bind-roles`")
        if spec.name not in UNSEEDABLE_PROVIDERS:
            _fail(str(e),
                  cause="a --classify-harness / --agentic-harness value is not "
                        "one this provider offers, or the provider has no "
                        "model set for a tier",
                  fix=f"re-run with the defaults — `sprintbaton providers add "
                      f"{spec.name} --classify-harness default "
                      f"--agentic-harness default` — or set the model variable "
                      f"named above")
        if bind:
            _fail(str(e),
                  cause=f"--bind-roles needs seeded agents, and {spec.name!r} "
                        f"has no seedable runtime or default model",
                  fix=f"register it without the flag (`sprintbaton providers "
                      f"add {spec.name}`), then create its agents by hand with "
                      f"`sprintbaton agents create --name <name> --harness "
                      f"<harness> --model <model id> --provider {spec.name}`")
        typer.echo(f"note: {e}")
        classify_runtime = agentic_runtime = None
        rows = []

    selected = harnesses_for_rows(rows) or (list(b.harnessNames) if b else [])
    try:
        provider, created, installs = container.provider_service.apply(
            spec, user_id, install_harnesses=selected,
            skip_install=skip_install, update_existing=update_existing)
    except ProviderError as e:
        _fail(str(e),
              cause="the provider name is not a built-in one, or the "
                    "credential it references does not exist",
              fix="use one of the names listed above, e.g. `sprintbaton "
                  "providers add anthropic --bind-roles`; `sprintbaton "
                  "credentials list` shows valid credential ids")

    deps = b.harnessDependencies if b else {}
    for harness_name, install in zip(selected, installs):
        dep = deps.get(harness_name)
        extra = dep.pipExtra if dep else None
        if install.attempted:
            typer.echo(f"installed sprintbaton[{extra}] for {harness_name}")
        elif extra and not install.already_satisfied:
            typer.echo(f"note: sprintbaton[{extra}] ({harness_name}) is not "
                       f"installed — run `pip install sprintbaton[{extra}]` (or "
                       f"rebuild the hosted image with that extra)")
    if provider.cliBinary and shutil.which(provider.cliBinary) is None:
        typer.echo(f"warning: CLI binary {provider.cliBinary!r} not found on PATH — "
                   f"install/authenticate it before running tasks on this provider")
    # A CLI runtime seeded into a hosted install writes rows that can never
    # resolve: a login session is not routable to a worker pod, so those
    # harnesses are tool-mode only (cli-subscription-auth-parity spec §4.6).
    # Warned, not refused — the project's standing posture for a configuration
    # that is wrong for this deployment but not malformed.
    if (settings.sprintbaton_mode != "tool"
            and "cli" in (classify_runtime, agentic_runtime)):
        which = [label for label, runtime in
                 (("--classify-harness", classify_runtime),
                  ("--agentic-harness", agentic_runtime)) if runtime == "cli"]
        typer.echo(f"warning: {' and '.join(which)} cli seeds rows on a "
                   f"login-only CLI harness, which cannot authenticate in "
                   f"hosted mode — tasks reaching them will be parked Blocked. "
                   f"Use `api`/`agent_sdk` here, or list a metered row after "
                   f"the CLI one in the binding chain.")

    if seed and rows:
        outcome = write_rows(rows, container.agent_definitions, user_id,
                             update_existing=update_existing)
        for row in outcome.created:
            typer.echo(f"seeded agent {row.name} = {row.harness_name} / "
                       f"{row.provider}/{row.model_id}")
        for row in outcome.updated:
            typer.echo(f"updated agent {row.name} = {row.harness_name} / "
                       f"{row.provider}/{row.model_id}")
        for row in outcome.unchanged:
            typer.echo(f"agent {row.name} already exists — left as is")

    verb = "registered" if created else ("updated" if update_existing else "already registered")
    typer.echo(f"{verb} provider {provider.name} (type={provider.providerType}, "
               f"id={provider.id})")

    # A user-layer hook install is the one setup step this feature cannot hide
    # (subprocess-cli-write-parity spec §4.4) — Codex keys hook trust on the
    # config file's absolute path, so a per-task clone can never accumulate one.
    for harness_name in selected:
        _install_user_hook(harness_name, container)

    for harness_name in selected:
        _print_advisories(harness_name)

    if bind:
        _bind_provider_roles(container, spec.name, user_id,
                             classify_runtime=classify_runtime,
                             agentic_runtime=agentic_runtime,
                             exec_rows=exec_rows_present(rows))


def _bind_provider_roles(container, provider: str, user_id: str, *,
                         classify_runtime: str, agentic_runtime: str,
                         exec_rows: bool) -> None:
    """Write the 13 action bindings + execution tier map, after checking that
    every row they point at exists. A binding naming a missing row would fail
    loudly at first dispatch, which is exactly the failure mode this spec
    removes — so it is caught here instead."""
    from sprintbaton.providers.seeding import bind_roles, binding_map

    bindings = binding_map(provider, classify_runtime=classify_runtime,
                           agentic_runtime=agentic_runtime, exec_rows=exec_rows)
    # Flatten both binding shapes to the set of row names they reference: a bare
    # name for an action, and `E0,E1:name;E3:other` for the tier map.
    wanted: set[str] = set()
    for value in bindings.values():
        for clause in value.split(";"):
            candidate = clause.rsplit(":", 1)[-1].strip()
            if candidate:
                wanted.add(candidate)
    missing = sorted(
        n for n in wanted
        if container.agent_definitions.find_one(
            {"name": n, "userId": user_id}) is None)
    if missing:
        _fail(f"cannot bind: no AgentDefinition named {', '.join(missing)}",
              cause="the agents these roles would be bound to were never "
                    "seeded for this runtime choice, or were deleted",
              fix=f"`sprintbaton providers add {provider} --classify-harness "
                  f"{classify_runtime} --agentic-harness {agentic_runtime} "
                  f"--bind-roles` seeds them and binds in one step")
    bind_roles(provider, container.user_service, user_id,
               classify_runtime=classify_runtime,
               agentic_runtime=agentic_runtime, exec_rows=exec_rows)
    typer.echo(f"bound all 13 task actions and the execution tier map to "
               f"{provider} (classify={classify_runtime}, "
               f"agentic={agentic_runtime})")
    # Records which provider `setup` and a bare `providers update` should
    # default to. Not a resolution input any more (§7.1).
    _upsert_env("SPRINTBATON_DEFAULT_PROVIDER", provider)


def _install_user_hook(harness_name: str, container) -> None:
    """Merge SprintBaton's guard hook into the user's own CLI hook config, for
    a harness whose dialect keeps its config at the user layer (spec §4.4).

    Additive and idempotent: the user's own entries are preserved, only a
    previous SprintBaton entry is replaced, and the file is backed up before the
    first write. The hook is inert outside SprintBaton runs (spec §4.3), so
    installing it changes nothing about the user's interactive sessions.
    """
    import json

    from sprintbaton.harness.subprocess_cli import merge_hook_config

    harness = container.harness_registry.names()
    if harness_name not in harness:
        return
    dialect = getattr(container.harness_registry.get(harness_name),
                      "hook_dialect", None)
    if dialect is None or dialect.config_scope != "user_once":
        return

    path = Path("~").expanduser() / dialect.config_relpath
    try:
        existing = json.loads(path.read_text()) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        typer.echo(f"warning: could not read {path} — leaving it untouched. "
                   f"{harness_name} write mode will run unguarded until the "
                   f"SprintBaton hook is installed there.", err=True)
        typer.echo("  likely cause: the file is not valid JSON, or is not "
                   "readable by this user", err=True)
        typer.echo(f"  to fix: repair or remove {path}, then re-run this "
                   f"`sprintbaton providers add` command", err=True)
        return
    if existing and not path.with_suffix(path.suffix + ".sprintbaton-bak").exists():
        path.with_suffix(path.suffix + ".sprintbaton-bak").write_text(
            json.dumps(existing, indent=2))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merge_hook_config(existing, dialect), indent=2))
    typer.echo(f"installed the SprintBaton guard hook into {path}")
    typer.echo(
        f"  ACTION REQUIRED: {dialect.name} only runs hooks you have trusted. "
        f"Run `{dialect.name}`, open /hooks, and trust the SprintBaton entry — "
        f"once. Until then, write-mode runs on {harness_name} proceed WITHOUT "
        f"guardrail enforcement and warn on every run.")


@providers_app.command("list")
def providers_list():
    """List built-in and registered providers, with their dependency status."""
    from sprintbaton.providers.registry import BUILTIN_PROVIDERS

    container = _container()
    user_id = container.settings.sprintbaton_user_id
    registered = {p.name: p for p in container.provider_service.list_for(user_id)}
    typer.echo("Built-in providers:")
    for name, b in sorted(BUILTIN_PROVIDERS.items()):
        row = registered.get(name)
        state = "registered" if row else "available"
        typer.echo(f"  {name}: binary={b.cliBinary or '(none)'} [{state}]")
        # One line per harness now that a provider backs harnesses with
        # different dependency footprints (multi-provider-parity spec §4.3/§7).
        for harness_name in b.harnessNames:
            satisfied = container.provider_service.dependencies_satisfied(
                name, harness_name)
            dep = b.harnessDependencies.get(harness_name)
            if dep is None or dep.pipExtra is None:
                deps = "ok (no extra)"
            else:
                deps = "installed" if satisfied else f"needs sprintbaton[{dep.pipExtra}]"
            typer.echo(f"      - {harness_name}: {deps}")
    extra = [p for n, p in registered.items() if n not in BUILTIN_PROVIDERS]
    if extra:
        typer.echo("Registered (custom pools):")
        for p in extra:
            typer.echo(f"  {p.name}: type={p.providerType} active={p.active} "
                       f"limits={len(p.tokenLimits)} id={p.id}")
    for p in registered.values():
        if p.tokenLimits or not p.active:
            state = "active" if p.active else f"INACTIVE until {p.inactiveUntil}"
            limits = "; ".join(f"{lim.maxTokens} tok/{lim.windowSeconds}s"
                               for lim in p.tokenLimits) or "(none)"
            typer.echo(f"  · {p.name}: {state}, limits: {limits}")


@providers_app.command("set-limit")
def providers_set_limit(
    name: str = typer.Argument(..., help="Registered provider name"),
    max_tokens: int = typer.Option(..., "--max-tokens",
                                   help="Token ceiling within the window"),
    window_seconds: int = typer.Option(..., "--window-seconds",
                                       help="Rolling window length in seconds"),
    replace: bool = typer.Option(False, "--replace",
                                 help="Replace existing limits instead of adding"),
):
    """Attach a self-imposed rolling-window token budget to a provider — the
    ceiling the fallback router enforces from recorded spend (agent-fallback
    spec §3.4)."""
    from sprintbaton.providers.service import ProviderError

    container = _container()
    try:
        provider = container.provider_service.set_token_limit(
            container.settings.sprintbaton_user_id, name,
            max_tokens=max_tokens, window_seconds=window_seconds, replace=replace)
    except ProviderError as e:
        _fail(str(e),
              cause="limits live on a registered provider row, and built-in "
                    "providers have none until they are added",
              fix=f"`sprintbaton providers add {name}`, then re-run this "
                  f"command; `sprintbaton providers list` shows what is "
                  f"registered")
    typer.echo(f"{name}: {len(provider.tokenLimits)} limit(s) — "
               f"{max_tokens} tokens / {window_seconds}s")


@providers_app.command("delete")
def providers_delete(name: str = typer.Argument(..., help="Registered provider name")):
    """Soft-delete a registered (non-built-in) provider by name."""
    from sprintbaton.providers.registry import BUILTIN_PROVIDERS

    container = _container()
    if name in BUILTIN_PROVIDERS and container.provider_service.get(
            container.settings.sprintbaton_user_id, name) is None:
        _fail(f"{name} is a built-in provider and was never registered — "
              f"nothing to delete",
              cause="built-in providers exist without a stored row, so there "
                    "is nothing to remove",
              fix=f"nothing is needed; to stop using {name}, bind the roles to "
                  f"another provider with `sprintbaton providers add "
                  f"<provider> --bind-roles`")
    if not container.provider_service.delete(
            container.settings.sprintbaton_user_id, name):
        _fail(f"no registered provider named {name!r}",
              cause="the name is misspelled, or the provider was already deleted",
              fix="`sprintbaton providers list` shows the registered names")
    typer.echo(f"deleted provider {name}")


# ----------------------------------------------------------- harness hooks

@app.command("harness-guard", hidden=True)
def harness_guard(
    workspace: str = typer.Option("", help="Workspace root the run is confined to"),
    answer_file: str = typer.Option("", help="The one permitted Write target (read-only mode)"),
    state_dir: str = typer.Option("", help="Per-run state dir for the blocked/exec-state files"),
    mode: str = typer.Option("", help="Guard ruleset: read_only | execution"),
    dialect: str = typer.Option(
        "claude", help="Hook wire format: claude | codex | gemini"),
    writable_root: list[str] = typer.Option(
        [], "--writable-root",
        help="A root a read-only run may Write/Edit inside (repeatable)"),
):
    """The hook entrypoint every CLI harness shells into (claude-code-cli spec
    §5.3, write-execution spec §5, subprocess-cli-write-parity spec §4.2): reads
    the hook input JSON from stdin, applies the same guard.py checks
    claude_agent_sdk enforces in-process, prints the hook-protocol decision JSON.

    `--dialect` selects the wire format only — one guard implementation, three
    wire formats. `claude` is the default so every existing call site is
    byte-identical.

    For the non-Claude dialects the run state arrives through the environment
    rather than these flags (spec §4.3), which is what keeps their hook config
    byte-identical across runs and therefore trust-stable. Hidden — only a
    harness's generated hook config invokes it."""
    import json
    import os
    import sys

    from sprintbaton.harness.claude_code_cli import (
        evaluate_hook_execution,
        evaluate_hook_post,
        evaluate_hook_read_only,
    )
    from sprintbaton.harness.subprocess_cli import (
        DIALECTS,
        GUARD_ANSWER_FILE_VAR,
        GUARD_MODE_VAR,
        GUARD_PROBE_FILE_VAR,
        GUARD_STATE_DIR_VAR,
        GUARD_WORKSPACE_VAR,
        GUARD_WRITABLE_ROOTS_VAR,
        evaluate_dialect_hook,
    )

    # Flags win; the environment is the channel the static hook configs use.
    workspace = workspace or os.environ.get(GUARD_WORKSPACE_VAR, "")
    answer_file = answer_file or os.environ.get(GUARD_ANSWER_FILE_VAR, "")
    state_dir = state_dir or os.environ.get(GUARD_STATE_DIR_VAR, "")
    mode = mode or os.environ.get(GUARD_MODE_VAR, "")
    if not writable_root:
        raw_roots = os.environ.get(GUARD_WRITABLE_ROOTS_VAR, "")
        writable_root = [r for r in raw_roots.split(os.pathsep) if r]

    # The capability probe's breadcrumb (spec §7.2): proves the hook executed
    # at all. Checked before anything else so a probe run needs no other state.
    probe_file = os.environ.get(GUARD_PROBE_FILE_VAR, "")
    if probe_file:
        try:
            Path(probe_file).write_text("1")
        except OSError:
            pass
        typer.echo(json.dumps({}))
        return

    if dialect == "claude":
        # claude_code_cli bakes run state into the hook command and omits
        # --mode for read-only runs, so an absent mode means read-only there —
        # not "not our run". Preserves the pre-dialect behavior exactly.
        mode = mode or "read_only"
    elif not mode:
        # A dialect whose config lives at the user layer fires during the
        # user's own interactive sessions too. No guard mode in the environment
        # means this is not a SprintBaton run: allow immediately and touch
        # nothing (spec §4.3). This inertness is what makes installing into the
        # user's own hooks.json acceptable at all.
        typer.echo(json.dumps({}))
        return

    hook_input = json.load(sys.stdin)

    if dialect != "claude":
        try:
            selected = DIALECTS[dialect]
        except KeyError:
            raise typer.BadParameter(
                f"unknown dialect {dialect!r} (known: claude, "
                f"{', '.join(sorted(DIALECTS))})") from None
        decision, exit_code = evaluate_dialect_hook(
            hook_input, selected, mode=mode, workspace=workspace,
            state_dir=state_dir, answer_file=answer_file,
            writable_roots=tuple(writable_root))
        typer.echo(json.dumps(decision))
        # Both documented deny channels, always — a CLI may honor either.
        raise typer.Exit(code=exit_code)

    if hook_input.get("hook_event_name") == "PostToolUse":
        decision = evaluate_hook_post(
            hook_input, state_dir=state_dir, workspace=workspace)
    elif mode == "execution":
        decision = evaluate_hook_execution(
            hook_input, workspace=workspace, state_dir=state_dir)
    else:
        decision = evaluate_hook_read_only(
            hook_input, workspace=workspace, answer_file=answer_file,
            state_dir=state_dir, writable_roots=tuple(writable_root))
    typer.echo(json.dumps(decision))


# ----------------------------------------------------------------- agents

@agents_app.command("create")
def agents_create(
    name: str = typer.Option(..., help="Unique agent name, referenced by SPRINTBATON_<ACTION>_AGENT"),
    harness: str = typer.Option(..., help="Registered harness name (see `sprintbaton agents harnesses`)"),
    model: str = typer.Option(..., help="Provider model id, e.g. claude-sonnet-5, glm-5.2"),
    provider: str = typer.Option("anthropic", help="Model provider (litellm-style segment)"),
    system_prompt: str = typer.Option(
        None, "--system-prompt",
        help="A system preamble prepended before whichever action template "
             "runs (prompts/templates/<name>.md) — model-specific standing "
             "instructions, reusable across actions. Never a replacement."),
    description: str = typer.Option(None, help="What this agent is for"),
):
    """Create a custom agent: a persisted harness + model (+ system preamble)
    recipe. Bind it to an action with SPRINTBATON_<ACTION>_AGENT, or with
    `sprintbaton providers add --bind-roles` for a whole provider at once."""
    from sprintbaton.entities.agent_definition import AgentDefinition
    from sprintbaton.harness.base import unusable_harness_reason

    container = _container()
    user_id = container.settings.sprintbaton_user_id
    if harness not in container.harness_registry.names():
        _fail(f"unknown harness: {harness}",
              cause=f"--harness must be a registered harness name: "
                    f"{', '.join(sorted(container.harness_registry.names()))}",
              fix="`sprintbaton agents harnesses` describes each one; re-run "
                  "with one of those names")
    # The same write-time refusal POST /agents makes (cli-subscription-auth-
    # parity spec §4.5): a harness that cannot be sandboxed in a deployment
    # whose runs must be isolated, or a login-only CLI off a tool-mode
    # install, is a definition that could never execute.
    refusal = unusable_harness_reason(
        container.harness_registry.get(harness),
        isolated=container.sandbox.isolated,
        local_login_sessions=container.settings.local_login_sessions)
    if refusal:
        _fail(refusal,
              cause="this harness cannot run on this kind of install, so an "
                    "agent built on it could never execute a task",
              fix="`sprintbaton agents harnesses` lists the harnesses; pick "
                  "the same provider's agent-SDK or single-shot one and "
                  "re-run `sprintbaton agents create --harness <name> ...`")
    known_providers = container.provider_service.known_names(user_id)
    if provider not in known_providers:
        _fail(f"unknown provider: {provider} (known: {sorted(known_providers)})",
              cause="--provider names a model provider that is neither "
                    "built in nor registered",
              fix=f"use one of the known names, or register this one first "
                  f"with `sprintbaton providers add {provider}`")
    if container.agent_definitions.find_one({"name": name, "userId": user_id}) is not None:
        _fail(f"an AgentDefinition named {name!r} already exists",
              cause="agent names are unique, and `agents create` never "
                    "overwrites one",
              fix=f"choose another --name, or remove the old one with "
                  f"`sprintbaton agents delete {name}` and re-run "
                  f"(`sprintbaton agents list` shows what exists)")
    # Fail at creation, not once per task inside the dispatch path
    # (agent-system-prompt spec §5) — PromptRegistry.get reads from disk.
    if system_prompt and not container.ctx.prompts.exists(system_prompt):
        expected = container.ctx.prompts.path_for(system_prompt)
        _fail(f"unknown system prompt template: {system_prompt!r} — expected "
              f"{expected}",
              cause="--system-prompt takes a template name, and no file with "
                    "that name exists in the prompt templates directory",
              fix=f"create {expected} with the preamble text and re-run, or "
                  f"drop --system-prompt to create the agent without one")

    definition = AgentDefinition(
        userId=user_id,
        name=name, harnessName=harness, modelProvider=provider,
        modelId=model, systemPromptName=system_prompt, description=description,
    )
    container.agent_definitions.save(definition)
    typer.echo(f"created {definition.id}: {name} = {harness} / {provider}/{model}")
    _print_advisories(harness, container.provider_service.base_url(provider, user_id))


def _print_advisories(harness: str, base_url: str = "") -> None:
    """Surface known upstream defects for a harness (subprocess-cli-write-parity
    spec §6.3), plus the gateway advisory when the agent's provider sets a
    baseUrl (hosted-sandbox-isolation spec §9.3). Deliberately non-blocking and
    on stderr: wiring an agent is a deliberate act, so the user is informed,
    not overruled."""
    from sprintbaton.harness.advisories import advisories_for, gateway_advisories

    for advisory in (*advisories_for(harness), *gateway_advisories(harness, base_url)):
        typer.echo("", err=True)
        typer.echo(advisory.render(), err=True)


@agents_app.command("list")
def agents_list():
    """List every AgentDefinition."""

    container = _container()
    definitions = container.agent_definitions.find(
        {"userId": container.settings.sprintbaton_user_id})
    if not definitions:
        typer.echo("no agent definitions yet — create one with `sprintbaton agents create`")
        return
    for d in definitions:
        # "(none)" not "(action default)": the action template always renders
        # now, so this column is only ever about the preamble (§6).
        preamble = d.systemPromptName or "(none)"
        typer.echo(f"{d.name}: harness={d.harnessName} model={d.modelProvider}/{d.modelId} "
                   f"system-prompt={preamble} id={d.id}")


@agents_app.command("delete")
def agents_delete(name: str = typer.Argument(..., help="AgentDefinition name")):
    """Soft-delete an AgentDefinition by name."""

    container = _container()
    definition = container.agent_definitions.find_one(
        {"name": name, "userId": container.settings.sprintbaton_user_id})
    if definition is None:
        _fail(f"no AgentDefinition named {name!r}",
              cause="the name is misspelled, or the agent was already deleted",
              fix="`sprintbaton agents list` shows the existing names")
    container.agent_definitions.soft_delete(definition.id)
    typer.echo(f"deleted {name} ({definition.id})")


@agents_app.command("harnesses")
def agents_harnesses():
    """List the registered harness names an AgentDefinition can reference.

    Harnesses carrying a known upstream defect are marked, so the list
    self-documents at the point of choosing (spec §6.3)."""
    from sprintbaton.harness.advisories import has_advisories

    marked = False
    for name in _container().harness_registry.names():
        if has_advisories(name):
            typer.echo(f"{name}  ⚠")
            marked = True
        else:
            typer.echo(name)
    if marked:
        typer.echo("\n⚠ = has known advisories; `sprintbaton agents create "
                   "--harness <name>` prints them.")


# ---------------------------------------------------------------- reports

MILLIS_PER_DAY = 86_400_000
_USAGE_HEADER = (f"{'Input':>12}{'Output':>12}{'Cache W':>12}"
                 f"{'Cache R':>12}{'Total':>12}")
_LABEL_WIDTH = 52


def _usage_cells(usage) -> str:
    return (f"{usage.inputTokens:>12,}{usage.outputTokens:>12,}"
            f"{usage.cacheCreationInputTokens:>12,}"
            f"{usage.cacheReadInputTokens:>12,}{usage.totalTokens:>12,}")


def _default_window(days: int | None, since: str | None, until: str | None,
                    default_days: int) -> tuple[int, int]:
    """Shared precedence (token-usage-reporting spec §6): explicit
    --since/--until wins; otherwise --days; otherwise the configured default
    (sprintbaton_report_default_days)."""
    from datetime import datetime, timezone

    from sprintbaton.entities.base import now_millis

    now = now_millis()

    def parse(value: str) -> int:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            _fail(f"cannot read {value!r} as a date",
                  cause="--since and --until take an ISO 8601 date or "
                        "date-time (UTC unless an offset is given)",
                  fix="re-run with e.g. `--since 2026-01-31` or `--since "
                      "2026-01-31T09:00:00`, or use `--days 7` instead")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)

    if since or until:
        return (parse(since) if since else now - default_days * MILLIS_PER_DAY,
                parse(until) if until else now)
    return now - (days or default_days) * MILLIS_PER_DAY, now


def _resolve_repo_id(container, user_id: str, repo_title: str | None) -> str | None:
    if not repo_title:
        return None
    repo = container.ctx.repo_repo.find_one(
        {"title": repo_title, "userId": user_id})
    if repo is None:
        _fail(f"no repository named {repo_title!r}",
              cause="--repo takes a member repository's name (not the project "
                    "title), matched exactly",
              fix="`sprintbaton project list` shows each project's repository "
                  "names; re-run with one of those, or drop --repo for every "
                  "repository")
    return repo.id


def _report_query(days, since, until, repo):
    """The shared setup every reports subcommand starts with. Reports are a
    pure read surface: log output is floored at quiet so the table is the
    only thing on stdout (warnings/errors still surface)."""

    _configure_logging("cli", quiet=True)
    container = _container()
    user_id = container.settings.sprintbaton_user_id
    since_ms, until_ms = _default_window(
        days, since, until, container.settings.sprintbaton_report_default_days)
    repo_id = _resolve_repo_id(container, user_id, repo)
    return container, user_id, since_ms, until_ms, repo_id


def _render_by_task_table(task_repo, breakdown) -> None:
    from sprintbaton.entities.usage import TokenUsage

    if not breakdown:
        typer.echo("no token usage recorded in this window.")
        return

    def task_total(states) -> TokenUsage:
        return sum((usage for by_agent in states.values()
                    for usage in by_agent.values()), TokenUsage())

    typer.echo(f"{'Task / state / agent':<{_LABEL_WIDTH}}{_USAGE_HEADER}")
    grand = TokenUsage()
    for task_id, states in sorted(
            breakdown.items(), key=lambda kv: -task_total(kv[1]).totalTokens):
        total = task_total(states)
        grand = grand + total
        # Title enrichment is presentation-only, best-effort (spec §8) — the
        # numbers never depend on the Task row still existing.
        task = task_repo.get(task_id)
        title = (task.title if task is not None else "(deleted task)") or ""
        label = f"{task_id}  {title}"[:_LABEL_WIDTH - 1]
        typer.echo(f"{label:<{_LABEL_WIDTH}}{_usage_cells(total)}")
        for action, by_agent in states.items():
            for model_id, usage in by_agent.items():
                sub = f"  {action} / {model_id}"[:_LABEL_WIDTH - 1]
                typer.echo(f"{sub:<{_LABEL_WIDTH}}{_usage_cells(usage)}")
    typer.echo(f"{'Total':<{_LABEL_WIDTH}}{_usage_cells(grand)}")


def _render_aggregate_table(key_header: str, aggregates) -> None:
    from sprintbaton.entities.usage import TokenUsage

    if not aggregates:
        typer.echo("no token usage recorded in this window.")
        return
    typer.echo(f"{key_header:<40}{'Events':>8}{'Tasks':>8}{_USAGE_HEADER}")
    total_usage = TokenUsage()
    total_events = 0
    all_tasks: set[str] = set()
    for key, aggregate in sorted(aggregates.items(),
                                 key=lambda kv: -kv[1].usage.totalTokens):
        total_usage = total_usage + aggregate.usage
        total_events += aggregate.event_count
        all_tasks |= aggregate.task_ids
        typer.echo(f"{key[:39]:<40}{aggregate.event_count:>8}"
                   f"{aggregate.task_count:>8}{_usage_cells(aggregate.usage)}")
    typer.echo(f"{'Total':<40}{total_events:>8}{len(all_tasks):>8}"
               f"{_usage_cells(total_usage)}")


def _render_timeseries_table(buckets, bucket: str) -> None:
    from datetime import datetime, timezone

    from sprintbaton.entities.usage import TokenUsage

    if not buckets:
        typer.echo("no token usage recorded in this window.")
        return
    fmt = "%Y-%m-%d %H:%M" if bucket == "hour" else "%Y-%m-%d"
    peak = max(b.usage.totalTokens for b in buckets) or 1
    typer.echo(f"{'Bucket (UTC)':<20}{'Events':>8}{'Total':>14}  ")
    total = TokenUsage()
    for entry in buckets:
        total = total + entry.usage
        label = datetime.fromtimestamp(
            entry.bucket_start_millis / 1000, tz=timezone.utc).strftime(fmt)
        bar = "█" * max(1, round(30 * entry.usage.totalTokens / peak))
        typer.echo(f"{label:<20}{entry.event_count:>8}"
                   f"{entry.usage.totalTokens:>14,}  {bar}")
    typer.echo(f"{'Total':<20}{sum(b.event_count for b in buckets):>8}"
               f"{total.totalTokens:>14,}")


@reports_app.callback(invoke_without_command=True)
def reports_default(ctx: typer.Context):
    """No subcommand => the explicitly requested default: per-task usage for
    the last week (sprintbaton_report_default_days)."""
    if ctx.invoked_subcommand is None:
        reports_by_task(days=None, since=None, until=None, repo=None, task_id=None)


@reports_app.command("by-task")
def reports_by_task(
    days: int = typer.Option(None, help="Lookback window in days (default: 7; --since/--until override)"),
    since: str = typer.Option(None, help="ISO date, e.g. 2026-07-01"),
    until: str = typer.Option(None, help="ISO date, e.g. 2026-07-19"),
    repo: str = typer.Option(None, help="Repository title — restrict to one repo"),
    task_id: str = typer.Option(None, "--task", help="Restrict to one task id"),
):
    """Token usage per task, broken down by task state (action) and agent (model)."""
    container, user_id, since_ms, until_ms, repo_id = _report_query(
        days, since, until, repo)
    breakdown = container.usage_reports.usage_by_task(
        user_id, since_ms, until_ms, repo_id=repo_id, task_id=task_id)
    _render_by_task_table(container.ctx.task_repo, breakdown)


@reports_app.command("by-agent")
def reports_by_agent(
    days: int = typer.Option(None, help="Lookback window in days (default: 7; --since/--until override)"),
    since: str = typer.Option(None, help="ISO date, e.g. 2026-07-01"),
    until: str = typer.Option(None, help="ISO date, e.g. 2026-07-19"),
    repo: str = typer.Option(None, help="Repository title — restrict to one repo"),
):
    """Total token usage per agent (model id) across the window."""
    container, user_id, since_ms, until_ms, repo_id = _report_query(
        days, since, until, repo)
    _render_aggregate_table("Agent (model)", container.usage_reports.usage_by_agent(
        user_id, since_ms, until_ms, repo_id=repo_id))


@reports_app.command("by-state")
def reports_by_state(
    days: int = typer.Option(None, help="Lookback window in days (default: 7; --since/--until override)"),
    since: str = typer.Option(None, help="ISO date, e.g. 2026-07-01"),
    until: str = typer.Option(None, help="ISO date, e.g. 2026-07-19"),
    repo: str = typer.Option(None, help="Repository title — restrict to one repo"),
):
    """Total token usage per pipeline phase (action) across the window."""
    container, user_id, since_ms, until_ms, repo_id = _report_query(
        days, since, until, repo)
    _render_aggregate_table("Task state (action)",
                            container.usage_reports.usage_by_task_state(
                                user_id, since_ms, until_ms, repo_id=repo_id))


@reports_app.command("timeseries")
def reports_timeseries(
    days: int = typer.Option(None, help="Lookback window in days (default: 7; --since/--until override)"),
    since: str = typer.Option(None, help="ISO date, e.g. 2026-07-01"),
    until: str = typer.Option(None, help="ISO date, e.g. 2026-07-19"),
    repo: str = typer.Option(None, help="Repository title — restrict to one repo"),
    bucket: str = typer.Option("day", help="Bucket width: hour, day, or week"),
):
    """Token usage over time, bucketed — the Console-usage-view shape."""
    container, user_id, since_ms, until_ms, repo_id = _report_query(
        days, since, until, repo)
    try:
        buckets = container.usage_reports.usage_timeseries(
            user_id, since_ms, until_ms, bucket=bucket, repo_id=repo_id)
    except KeyError as e:
        _fail(str(e.args[0]),
              cause="--bucket is not one of the supported bucket widths",
              fix="re-run with one of those, e.g. `sprintbaton reports "
                  "timeseries --bucket day`")
    _render_timeseries_table(buckets, bucket)


def main() -> None:
    """The console-script entry point: `app()`, with the one error that can
    surface from any command — an optional package this install does not
    carry — reported like every other CLI error rather than as a traceback."""
    from sprintbaton.dependencies import MissingDependencyError

    from pydantic import ValidationError

    try:
        app()
    except ValidationError as e:
        if e.title != "Settings":
            raise
        fields = ", ".join(sorted({str(err["loc"][0]).upper() for err in e.errors()
                                   if err.get("loc")}))
        _report_error(
            f"invalid configuration: {fields or e}",
            cause="a setting in the environment or in ./.env has a value of "
                  "the wrong type (for example text where a number or "
                  "true/false is expected)",
            fix="correct or unset the variable(s) named above — the README's "
                "configuration reference lists every setting and its default")
        raise SystemExit(1)
    except MissingDependencyError as e:
        _report_error(
            str(e).split(" Install it with")[0],
            cause="SprintBaton installs no provider SDK, storage client or API "
                  "server by default — each is an optional extra, and the "
                  "selected mode, backend or agent needs this one",
            fix=f"`pip install 'sprintbaton[{e.extra}]'` (hosted images: add "
                f"{e.extra!r} to the {e.build_arg} build argument and rebuild)")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
