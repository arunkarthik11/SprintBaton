FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY sprintbaton/ sprintbaton/

# What the image contains is two build arguments (pluggable-hosted-backends
# spec §6). The defaults ARE the published image: every storage client, so
# any backend combination is chosen by configuration alone, and the Anthropic
# provider packages only. An operator needing other provider SDKs or a smaller
# storage set builds their own image from this same file, e.g.
#   --build-arg PROVIDER_EXTRAS=openai-agents,openai \
#   --build-arg STORAGE_EXTRAS=postgres,gcs,redis,api
# Build the worker and Dockerfile.sandbox through docker-bake.hcl so both get
# the same PROVIDER_EXTRAS and CLAUDE_AGENT_SDK_VERSION.
ARG STORAGE_EXTRAS="mongo,postgres,s3,gcs,azure,redis,api,otlp"
ARG PROVIDER_EXTRAS="anthropic,claude-agent-sdk"
# --only-binary for claude-agent-sdk is load-bearing, not tidiness: the wheel
# ships a self-contained Claude Code executable in claude_agent_sdk/_bundled/,
# which SubprocessCLITransport._find_cli prefers over any PATH lookup. The
# sdist does NOT (it carries only a .gitignore there), so an sdist fallback
# builds a green image whose every advisory-role call dies at runtime with
# CLINotFoundError. The flag is inert when claude-agent-sdk is not requested.
# The sandbox image copies its `claude` binary out of the same wheel; build
# both with the same CLAUDE_AGENT_SDK_VERSION or every sandboxed run fails its
# version handshake (hosted-sandbox-isolation §7.2).
# After installing, every requested extra must import, and a requested
# claude-agent-sdk must carry a bundled CLI that runs on this image's
# architecture (the wheel is arch-specific: manylinux_2_17_{x86_64,aarch64}).
ARG CLAUDE_AGENT_SDK_VERSION=""
RUN set -eu; \
    extras="$(echo "${STORAGE_EXTRAS},${PROVIDER_EXTRAS}" | tr -d ' ' | sed 's/,,*/,/g; s/^,//; s/,$//')"; \
    pin=""; \
    case ",${PROVIDER_EXTRAS}," in \
      *,claude-agent-sdk,*) [ -z "${CLAUDE_AGENT_SDK_VERSION}" ] || pin="claude-agent-sdk==${CLAUDE_AGENT_SDK_VERSION}" ;; \
    esac; \
    pip install --no-cache-dir --only-binary claude-agent-sdk ".${extras:+[${extras}]}" ${pin}; \
    python -m sprintbaton.extras --check "${extras}"

# One var selects Mongo + S3/MinIO + Redis defaults for every concern;
# per-concern SPRINTBATON_<CONCERN>_BACKEND vars still override individually
# (zero-infra-storage spec §6).
ENV SPRINTBATON_MODE=hosted

# Workspaces for cloned repositories. Only the worker ever sees them: runs get
# a copy in the sandbox pod, never this volume (hosted-sandbox-isolation §6).
RUN mkdir -p /app/workspaces

# The egress/credential broker the sandbox pod's runs reach (§8.2).
EXPOSE 8081

CMD ["sprintbaton", "serve"]
