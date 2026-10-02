"""Pluggable model-provider registration and quota-pool availability
(provider-registration spec, agent-fallback-limits spec)."""

from sprintbaton.providers.availability import ProviderAvailabilityService
from sprintbaton.providers.registry import BUILTIN_PROVIDERS, BuiltinProvider
from sprintbaton.providers.service import (
    InstallResult,
    ProviderService,
    ProviderSpec,
    parse_provider_manifest,
)

__all__ = [
    "BUILTIN_PROVIDERS",
    "BuiltinProvider",
    "InstallResult",
    "ProviderAvailabilityService",
    "ProviderService",
    "ProviderSpec",
    "parse_provider_manifest",
]
