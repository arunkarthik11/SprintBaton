"""The pip extras an image can be built with, and what each must make
importable (pluggable-hosted-backends spec §5, §6).

`python -m sprintbaton.extras --check <comma-separated extras>` is the image
build's assertion: every requested extra imports, and when `claude-agent-sdk`
is requested its wheel really carries the bundled `claude` executable (the
sdist does not, and a fallback to it builds a green image that fails every
run). An unknown extra name fails the build. Stdlib only at import time.
"""

import argparse
import importlib
import pathlib
import subprocess
import sys

STORAGE_EXTRAS = ("mongo", "postgres", "s3", "gcs", "azure", "redis", "api", "otlp")
PROVIDER_EXTRAS = ("anthropic", "claude-agent-sdk", "openai", "openai-agents",
                   "google-genai", "google-adk", "openhands")

# Defaults of the Dockerfile build arguments of the same names (§6).
DEFAULT_STORAGE_EXTRAS = STORAGE_EXTRAS
DEFAULT_PROVIDER_EXTRAS = ("anthropic", "claude-agent-sdk")

EXTRA_MODULES: dict[str, tuple[str, ...]] = {
    "mongo": ("pymongo", "opentelemetry.instrumentation.pymongo"),
    "postgres": ("psycopg", "psycopg_pool"),
    "s3": ("boto3",),
    "gcs": ("google.cloud.storage",),
    "azure": ("azure.storage.blob", "azure.identity"),
    "redis": ("redis",),
    "api": ("fastapi", "uvicorn", "bcrypt"),
    "otlp": ("opentelemetry.exporter.otlp.proto.grpc",),
    "anthropic": ("anthropic",),
    "claude-agent-sdk": ("claude_agent_sdk",),
    "openai": ("openai",),
    "openai-agents": ("agents",),
    "google-genai": ("google.genai",),
    "google-adk": ("google.adk",),
    "openhands": ("openhands.sdk",),
}

# The pip package each provider extra installs, for startup messages (§4.9).
PROVIDER_PACKAGES: dict[str, str] = {
    "anthropic": "anthropic",
    "claude-agent-sdk": "claude-agent-sdk",
    "openai": "openai",
    "openai-agents": "openai-agents",
    "google-genai": "google-genai",
    "google-adk": "google-adk",
    "openhands": "openhands-ai",
}


def parse(value: str) -> list[str]:
    return [e.strip() for e in value.split(",") if e.strip()]


def check(extras: list[str]) -> list[str]:
    """Problems with an installed extra set; empty when all is well."""
    problems = []
    for extra in extras:
        modules = EXTRA_MODULES.get(extra)
        if modules is None:
            problems.append(f"unknown extra {extra!r}; known: {', '.join(EXTRA_MODULES)}")
            continue
        for module in modules:
            try:
                importlib.import_module(module)
            except Exception as e:  # noqa: BLE001 — any failure fails the build
                problems.append(f"extra {extra!r}: cannot import {module}: {e}")
    if "claude-agent-sdk" in extras and not problems:
        import claude_agent_sdk

        cli = pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        if not cli.is_file():
            problems.append(f"claude-agent-sdk has no bundled CLI at {cli} "
                            "(installed from the sdist?)")
        else:
            version = subprocess.run([str(cli), "--version"], check=True,
                                     capture_output=True, text=True).stdout.strip()
            print(f"bundled Claude Code: {version}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sprintbaton.extras")
    parser.add_argument("--check", required=True,
                        help="comma-separated extras that must import")
    args = parser.parse_args(argv)
    extras = parse(args.check)
    problems = check(extras)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if not problems:
        print(f"extras ok: {', '.join(extras) or '(none)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
