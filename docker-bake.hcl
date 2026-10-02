# The one build definition for the worker and sandbox images
# (pluggable-hosted-backends spec §6). Both targets read the same variables, so
# the worker's claude-agent-sdk and the sandbox's `claude` binary cannot be
# built out of step.
#
#   docker buildx bake                                   # the published default
#   PROVIDER_EXTRAS=openai-agents,openai \
#   STORAGE_EXTRAS=postgres,gcs,redis,api \
#   REGISTRY=registry.example.com/me TAG=custom docker buildx bake --push
#
# The defaults below must match the ARG defaults in Dockerfile and
# Dockerfile.sandbox (asserted by tests/test_image_build.py).

variable "REGISTRY" {
  default = "ghcr.io/sprintbaton"
}

variable "TAG" {
  default = "dev"
}

# Every storage client, so storage is chosen by configuration alone.
variable "STORAGE_EXTRAS" {
  default = "mongo,postgres,s3,gcs,azure,redis,api,otlp"
}

# The Anthropic provider packages only.
variable "PROVIDER_EXTRAS" {
  default = "anthropic,claude-agent-sdk"
}

# Empty = the latest release at build time. Pin it for reproducible builds.
variable "CLAUDE_AGENT_SDK_VERSION" {
  default = ""
}

group "default" {
  targets = ["worker", "sandbox"]
}

target "_common" {
  context = "."
  args = {
    PROVIDER_EXTRAS          = PROVIDER_EXTRAS
    CLAUDE_AGENT_SDK_VERSION = CLAUDE_AGENT_SDK_VERSION
  }
}

target "worker" {
  inherits   = ["_common"]
  dockerfile = "Dockerfile"
  args = {
    STORAGE_EXTRAS = STORAGE_EXTRAS
  }
  tags = ["${REGISTRY}/sprintbaton:${TAG}"]
}

target "sandbox" {
  inherits   = ["_common"]
  dockerfile = "Dockerfile.sandbox"
  tags       = ["${REGISTRY}/sprintbaton-sandbox:${TAG}"]
}
