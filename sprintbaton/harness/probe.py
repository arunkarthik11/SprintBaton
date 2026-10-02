"""The hook-capability probe (subprocess-cli-write-parity-and-advisories spec §7).

Some CLIs are known to sometimes not execute a configured hook at all — Codex's
`codex exec` skips hooks recorded as trusted (openai/codex#32491), reported on
Windows with Linux behavior unknown. Encoding a guess about that would be wrong
on one platform and would rot on both, so each install **measures** whether its
binary actually runs our hook.

Two properties this buys:

  * A per-install fact replaces an assertion the spec would have to keep current.
  * It self-heals — the day upstream fixes the bug, the probe passes and write
    mode switches on with no release from us.

The probe deliberately never passes `--dangerously-bypass-hook-trust` (spec
§2.3): it must measure the configuration we actually run in, and a probe that
only passed under a flag we refuse to use would be measuring nothing.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

# The probe result is a property of (binary, version) — not of a task, a project
# or a user — so it caches at the install level and re-runs when either changes.
_CACHE_DIRNAME = "hook-probe"
_PROBE_PROMPT = (
    "List the files in the current directory using your shell tool, then stop. "
    "Do not write any file.")


def _cache_root() -> Path:
    root = os.environ.get("SPRINTBATON_LOCAL_STORAGE_ROOT", "~/.sprintbaton")
    return Path(root).expanduser() / _CACHE_DIRNAME


def _binary_version(binary: str, timeout_seconds: int = 20) -> str:
    """A version string to key the cache on. Any failure yields "" — which
    still caches, just under a less specific key."""
    try:
        completed = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            timeout=timeout_seconds)
        return (completed.stdout or completed.stderr or "").strip()[:200]
    except (OSError, subprocess.SubprocessError):
        return ""


def _cache_path(binary: str, dialect_name: str, version: str) -> Path:
    from hashlib import sha256

    key = sha256(f"{binary}\0{dialect_name}\0{version}".encode()).hexdigest()[:16]
    return _cache_root() / f"{dialect_name}-{key}.json"


def read_cached(binary: str, dialect_name: str) -> bool | None:
    version = _binary_version(binary)
    try:
        data = json.loads(_cache_path(binary, dialect_name, version).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    result = data.get("hooks_execute")
    return result if isinstance(result, bool) else None


def _write_cache(binary: str, dialect_name: str, version: str,
                 result: bool) -> None:
    path = _cache_path(binary, dialect_name, version)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "hooks_execute": result,
            "binary": binary,
            "version": version,
            "checked_at": int(time.time()),
        }))
    except OSError:  # a cache we cannot write is a slow probe, not a failure
        log.debug("could not cache hook-probe result", exc_info=True)


def probe_hook_capability(binary: str, dialect, *, timeout_seconds: int = 120,
                          runner=None) -> bool:
    """Does `binary` actually execute a configured hook?

    Writes the dialect's hook config into a scratch dir whose hook drops a
    breadcrumb, runs one trivial prompt that forces a tool call, and reports
    whether the breadcrumb appeared. Cached per (binary, version).

    `runner` is a test seam with subprocess.run's signature.
    """
    from sprintbaton.harness.subprocess_cli import (
        GUARD_MODE_VAR,
        GUARD_PROBE_FILE_VAR,
        GUARD_STATE_DIR_VAR,
        GUARD_WORKSPACE_VAR,
        build_hook_config,
    )

    version = _binary_version(binary)
    cached = read_cached(binary, dialect.name)
    if cached is not None:
        return cached

    run = runner or subprocess.run
    with tempfile.TemporaryDirectory(prefix="sprintbaton-hook-probe-") as tmp:
        workspace = Path(tmp) / "ws"
        workspace.mkdir()
        breadcrumb = Path(tmp) / "hook-fired"
        config_path = workspace / dialect.config_relpath
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(build_hook_config(dialect), indent=2))

        env = dict(os.environ)
        env[GUARD_MODE_VAR] = "read_only"
        env[GUARD_WORKSPACE_VAR] = str(workspace)
        env[GUARD_STATE_DIR_VAR] = tmp
        env[GUARD_PROBE_FILE_VAR] = str(breadcrumb)

        try:
            run([binary, "-p", _PROBE_PROMPT], cwd=str(workspace), env=env,
                capture_output=True, text=True, timeout=timeout_seconds)
        except (OSError, subprocess.SubprocessError) as e:
            # A binary we cannot invoke is not a hook problem; report it as
            # "unenforced" so the caller warns rather than silently trusting.
            log.warning("hook-capability probe could not run %s: %s", binary, e)
            return False

        result = breadcrumb.exists()

    _write_cache(binary, dialect.name, version, result)
    log.info("hook-capability probe for %s: hooks %s", binary,
             "execute" if result else "DO NOT execute",
             extra={"event": "harness_probe", "harness": dialect.name,
                    "hooks_execute": result})
    return result
