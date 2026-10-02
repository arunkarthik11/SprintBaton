"""The harness-facing half of the seam (hosted-sandbox-isolation spec §5.3, §6).

A harness never talks to a `Sandbox` directly: it binds its task spec's
workspace with `bind_workspace`, which opens (or re-attaches to) the task's
session, puts the workspace tree, and — for a run that can write — collects the
run's changes and applies them to the worker's authoritative clone before the
harness computes its diff (invariant 7). Identical call sequence in both modes;
under the tool-mode passthrough the put/collect/apply steps are no-ops.
"""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sprintbaton.sandbox.base import (
    ChangeSetRejected,
    EgressGrant,
    RunResult,
    RunSpec,
    Sandbox,
    SandboxSession,
    TreeSnapshot,
)
from sprintbaton.sandbox.local import LOCAL_SANDBOX

if TYPE_CHECKING:
    from sprintbaton.harness.base import HarnessTaskSpec
    from sprintbaton.sandbox.broker import EgressBroker, UpstreamCredential

log = logging.getLogger(__name__)

DEFAULT_GIT_DEPTH = 50
DEFAULT_MAX_CHANGESET_BYTES = 100 * 1024 * 1024
# Owner/task used when a spec carries none (direct harness calls in tests and
# ad-hoc runs). A hosted task always carries both (models/agents.py).
ADHOC_OWNER = "local"
ADHOC_TASK = "adhoc"


@dataclass
class SandboxRuntime:
    """What a harness needs to reach the sandbox: the sandbox itself, the
    egress broker that mints run tokens (§8.2, hosted only), and the transfer
    limits. The default is tool mode's passthrough with no broker."""

    sandbox: Sandbox = field(default_factory=lambda: LOCAL_SANDBOX)
    broker: EgressBroker | None = None
    git_depth: int = DEFAULT_GIT_DEPTH
    max_changeset_bytes: int = DEFAULT_MAX_CHANGESET_BYTES

    @property
    def isolated(self) -> bool:
        return bool(getattr(self.sandbox, "isolated", False))


LOCAL_RUNTIME = SandboxRuntime()


def tree_name_for(path: str) -> str:
    """A stable tree name per mount path: one tree per clone, reused by every
    role of the task that binds the same clone (§6.1)."""
    return "t-" + hashlib.sha256(posixpath.normpath(path).encode()).hexdigest()[:16]


class BoundWorkspace:
    """One harness run's view of its session."""

    def __init__(self, runtime: SandboxRuntime, session: SandboxSession,
                 spec: HarnessTaskSpec, tree: str | None, root: str | None,
                 collect_paths: list[str] | None, write_capable: bool):
        self.runtime = runtime
        self.session = session
        self.spec = spec
        self.tree = tree
        self.root = root                  # session path of the workspace, or None
        self.collect_paths = collect_paths
        self.write_capable = write_capable
        # Credential values injected into this binding's runs (§8.3 env
        # delivery). The change set is scanned for each before it is applied.
        self._secrets: set[str] = set()

    @property
    def isolated(self) -> bool:
        return self.runtime.isolated

    def register_secret(self, value: str) -> None:
        if value:
            self._secrets.add(value)

    # ------------------------------------------------------------------- runs

    def mint_egress(self, credential: UpstreamCredential | None = None,
                    extra_hosts: tuple[str, ...] = ()) -> EgressGrant | None:
        """A run token for one run (§8.2) — None under the passthrough, whose
        runs keep the host's own network, and when no broker is configured
        (the run then has no network at all)."""
        broker = self.runtime.broker
        if not self.isolated or broker is None:
            return None
        token = broker.mint(owner_id=self.spec.owner_id or ADHOC_OWNER,
                            task_id=self.spec.task_id or ADHOC_TASK,
                            credential=credential, extra_hosts=extra_hosts)
        return EgressGrant(token=token)

    def revoke(self, grant: EgressGrant | None) -> None:
        if grant is not None and self.runtime.broker is not None:
            self.runtime.broker.revoke(grant.token)

    def shell(self, command: str, *, cwd: str, timeout_seconds: int) -> RunResult:
        """A model-issued shell command (§7.1). Egress is package registries
        only: the tools harnesses make their model calls in the worker, so no
        credential ever reaches these runs (§8.3)."""
        grant = self.mint_egress()
        try:
            return self.session.run(RunSpec(
                argv=["/bin/sh", "-c", command], cwd=cwd,
                env=self.session.tool_env(), timeout_seconds=timeout_seconds,
                egress=grant))
        finally:
            self.revoke(grant)

    # ---------------------------------------------------------------- scratch

    @contextmanager
    def scratch(self, prefix: str = "run") -> Iterator[str]:
        path = self.session.make_scratch(prefix)
        try:
            yield path
        finally:
            self.session.discard_scratch(path)

    def read_text(self, path: str) -> str | None:
        """A file's text, or None when it does not exist / is not a file."""
        try:
            return self.session.read_file(path).decode(errors="replace")
        except (OSError, ValueError, RuntimeError):
            return None

    # ------------------------------------------------------------- sync back

    def sync_back(self) -> None:
        """Collect this run's changes and apply them to the worker's clone
        (§6.4) — after every run that can write, before any diff or commit."""
        if not (self.write_capable and self.isolated and self.tree):
            return
        from sprintbaton.sandbox.transfer import apply_changeset, scan_for_secrets

        changes = self.session.collect(self.tree, self.collect_paths)
        try:
            scan_for_secrets(changes, self._secrets)
            applied = apply_changeset(self.spec.workspace_path, changes,
                                      max_bytes=self.runtime.max_changeset_bytes,
                                      allowed=self.collect_paths)
        except ChangeSetRejected:
            # The session's tree now disagrees with the worker's; drop it so
            # the next role rebuilds it from the authoritative clone (inv. 7).
            self.session.drop_tree(self.tree)
            raise
        log.info("sandbox changes applied", extra={
            "task_id": self.spec.task_id, "files": applied})


    def sync_back_quietly(self) -> None:
        """`sync_back` on an exception path: never masks the exception that is
        already propagating (a guard hard stop, most often)."""
        try:
            self.sync_back()
        except Exception:
            log.warning("sandbox sync-back failed on an aborted run",
                        extra={"task_id": self.spec.task_id}, exc_info=True)


@contextmanager
def bind_workspace(runtime: SandboxRuntime | None,
                   spec: HarnessTaskSpec) -> Iterator[BoundWorkspace]:
    """Context-manager form of `open_binding`. Nothing is torn down on exit:
    the session outlives the run (§6.1) and is closed at Shipped/Blocked."""
    yield open_binding(runtime, spec)


def open_binding(runtime: SandboxRuntime | None,
                 spec: HarnessTaskSpec) -> BoundWorkspace:
    """Open the task's session and put its workspace tree (§6.2).

    The tree mode follows the spec: `rw` for a write-capable run (execution,
    conflict resolution), `ro` otherwise — with `writable_paths` as the `ro`
    tree's writable subpaths (the init pass, §6.2). A writable root equal to
    the workspace makes the whole tree writable (the project pass)."""
    runtime = runtime or LOCAL_RUNTIME
    session = runtime.sandbox.open_session(spec.owner_id or ADHOC_OWNER,
                                           spec.task_id or ADHOC_TASK)
    tree = root = None
    collect_paths: list[str] | None = None
    write_capable = not spec.read_only
    if spec.workspace_path:
        workspace = os.path.normpath(spec.workspace_path)
        writable: list[str] = []
        whole_tree_writable = False
        if spec.read_only:
            for raw in spec.writable_paths:
                rel = os.path.relpath(os.path.normpath(raw), workspace)
                if rel == ".":
                    whole_tree_writable = True
                elif rel.startswith(".."):
                    raise ValueError(
                        f"writable path {raw!r} is outside the workspace {workspace!r}; "
                        f"the sandbox binds one tree per run")
                else:
                    writable.append(rel.replace(os.sep, "/"))
            if writable or whole_tree_writable:
                write_capable = True
                collect_paths = None if whole_tree_writable else writable
        mode = "rw" if (not spec.read_only or whole_tree_writable) else "ro"
        tree = tree_name_for(workspace)
        root = session.put_tree(tree, TreeSnapshot(
            source=workspace, writable=tuple(writable),
            git_depth=runtime.git_depth), mode)
    return BoundWorkspace(runtime, session, spec, tree, root, collect_paths,
                          write_capable)
