"""ProviderService — persisted-provider CRUD plus the dependency-check/install
orchestration shared by `sprintbaton providers add|list|delete` and the
`/providers` API routes (provider-registration spec §4, §7, §8).

Security boundary (spec §13): the only thing `ensure_installed` ever passes to
pip is `sprintbaton[<extra>]` where `<extra>` comes from the built-in,
code-owned table — never a value read verbatim from a manifest or API body.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, field_validator

from sprintbaton.entities.enums import CredentialProvider
from sprintbaton.entities.provider import Provider, SelfImposedLimit
from sprintbaton.providers.registry import BUILTIN_PROVIDERS, builtin
from sprintbaton.storage.base import EntityDAO
from sprintbaton.users.credentials import CredentialService

log = logging.getLogger(__name__)

MANIFEST_API_VERSION = "sprintbaton/v1"
MANIFEST_KIND = "Provider"


class ProviderError(ValueError):
    """A validation failure registering a provider — 422 at the API boundary,
    a plain error message from the CLI."""


class ProviderCredentialSpec(BaseModel):
    model_config = {"extra": "forbid"}
    token: str = ""


class ProviderSpec(BaseModel):
    """The validated registration payload (spec §8.3) — also the POST
    /providers request body. `name` must be a built-in provider name (the
    only creatable set this spec ships — §8.2 step 1); the row's type/harness/
    binary come from the built-in table unless explicitly overridden here."""

    model_config = {"extra": "forbid"}

    name: str
    cliBinary: str | None = None            # optional override of the built-in default
    credential: ProviderCredentialSpec | None = None
    tokenLimits: list[SelfImposedLimit] = []  # agent-fallback spec §3.4 / §5
    # An endpoint other than the provider type's default (hosted-sandbox-
    # isolation spec §9.3) — manifest spec.baseUrl / `providers add --base-url`.
    baseUrl: str | None = None

    @field_validator("baseUrl")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        return validate_base_url(value)


def validate_base_url(value: str | None) -> str | None:
    """https:// only (spec §9.3): the URL receives the owner's credential, so a
    plaintext endpoint would put it on the wire in the clear."""
    if value is None or not value.strip():
        return None
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"baseUrl must be an https:// URL, got {value!r}")
    if parts.username or parts.password:
        raise ValueError("baseUrl must not embed credentials")
    return value


@dataclass(frozen=True)
class InstallResult:
    attempted: bool
    already_satisfied: bool


def parse_provider_manifest(text: str) -> ProviderSpec:
    """Parse the k8s-style Provider manifest (spec §8.3) into a ProviderSpec.
    Shares the envelope shape onboarding.parse_repository_manifest established."""
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ProviderError("manifest must be a YAML mapping")
    if raw.get("apiVersion") != MANIFEST_API_VERSION:
        raise ProviderError(
            f"unsupported apiVersion: {raw.get('apiVersion')!r} "
            f"(expected {MANIFEST_API_VERSION!r})")
    if raw.get("kind") != MANIFEST_KIND:
        raise ProviderError(
            f"unsupported kind: {raw.get('kind')!r} (expected {MANIFEST_KIND!r})")
    name = (raw.get("metadata") or {}).get("name")
    if not name:
        raise ProviderError("metadata.name is required")
    spec = dict(raw.get("spec") or {})
    payload = {"name": name, **spec}
    try:
        return ProviderSpec.model_validate(payload)
    except ValueError as e:
        raise ProviderError(f"invalid provider manifest: {e}") from e


class ProviderService:
    def __init__(self, provider_repo: EntityDAO[Provider],
                 credentials: CredentialService, *, hosted: bool = False):
        self._repo = provider_repo
        self._credentials = credentials
        # Hosted-mode default: never self-modify a running pod's site-packages
        # (spec §12) — register + report, the operator bakes the extra into the
        # image build.
        self._hosted = hosted

    # ---------------------------------------------------------------- lookup

    def known_names(self, user_id: str) -> set[str]:
        """Built-in table keys ∪ this user's persisted Provider names
        (spec §4.1, §10)."""
        return set(BUILTIN_PROVIDERS) | {
            p.name for p in self._repo.find({"userId": user_id}) if p.name
        }

    def get(self, user_id: str, name: str) -> Provider | None:
        return self._repo.find_one({"name": name, "userId": user_id})

    def provider_type(self, name: str, user_id: str) -> str:
        """The litellm/harness routing string a harness guards on, for a
        provider name. A persisted row wins; otherwise the built-in table;
        otherwise the name itself (backward compat — modelProvider="anthropic"
        with no row still resolves to type "anthropic")."""
        row = self.get(user_id, name)
        if row is not None and row.providerType:
            return row.providerType
        b = builtin(name)
        return b.providerType if b is not None else name

    def base_url(self, name: str, user_id: str) -> str:
        """Provider.baseUrl for a provider name, "" for the type's default."""
        row = self.get(user_id, name)
        return (row.baseUrl or "") if row is not None else ""

    def list_for(self, user_id: str) -> list[Provider]:
        return self._repo.find({"userId": user_id})

    # --------------------------------------------------------------- install

    def ensure_installed(self, spec_name: str, harness_name: str, *,
                         skip: bool = False) -> InstallResult:
        """Install one harness's optional pip dependency, if it has one and
        isn't already importable (spec §4.3/§7.2). Keyed by harness name now
        that one provider backs harnesses with different footprints — a no-op
        for a harness absent from the provider's harnessDependencies map (or
        with no pipExtra), which covers every pure-subprocess wrapper
        (codex_cli/gemini_cli)."""
        dep = self._dependency(spec_name, harness_name)
        if dep is None or dep.pipExtra is None:
            return InstallResult(attempted=False, already_satisfied=True)
        if dep.importCheckModule and importlib.util.find_spec(dep.importCheckModule) is not None:
            return InstallResult(attempted=False, already_satisfied=True)
        if skip or self._hosted:
            return InstallResult(attempted=False, already_satisfied=False)
        # sys.executable, never a bare "pip" — the same interpreter/venv
        # sprintbaton runs in; the ONLY value that reaches pip is the
        # code-owned extra name, never user input (spec §13).
        subprocess.run(
            [sys.executable, "-m", "pip", "install", f"sprintbaton[{dep.pipExtra}]"],
            check=True,
        )
        return InstallResult(attempted=True, already_satisfied=False)

    def dependencies_satisfied(self, name: str, harness_name: str) -> bool:
        """Is one harness's optional dependency importable? True when the
        harness declares no dependency (spec §4.3)."""
        dep = self._dependency(name, harness_name)
        if dep is None or dep.importCheckModule is None:
            return True
        return importlib.util.find_spec(dep.importCheckModule) is not None

    @staticmethod
    def _dependency(name: str, harness_name: str):
        """The HarnessDependency a built-in provider declares for one harness,
        or None if it declares none for it."""
        b = builtin(name)
        if b is None:
            return None
        return b.harnessDependencies.get(harness_name)

    # ----------------------------------------------------------------- apply

    def apply(self, spec: ProviderSpec, user_id: str, *,
              install_harnesses: list[str] | None = None,
              skip_install: bool = False,
              update_existing: bool = True,
              ) -> tuple[Provider, bool, list[InstallResult]]:
        """Register a provider from a validated spec (spec §8.2). Installs the
        optional dependency of each harness named in `install_harnesses`
        (multi-provider-parity spec §4.3/§7 — the CLI derives the selection from
        the two runtime flags); an empty/None selection installs nothing.

        `update_existing=False` makes the whole call create-if-absent
        (provider-setup-cli spec §4.6): an existing row's fields, token limits
        and credential are left exactly as they are, so `providers add` run
        twice with identical arguments is a byte-identical no-op (invariant 5).
        `providers update` is the deliberate mutation and passes True.

        Returns (provider, created, install_results) — one InstallResult per
        requested harness, in order."""
        b = builtin(spec.name)
        if b is None:
            raise ProviderError(
                f"unknown provider: {spec.name!r} "
                f"(registerable built-ins: {', '.join(sorted(BUILTIN_PROVIDERS))})")

        installs = [self.ensure_installed(spec.name, h, skip=skip_install)
                    for h in (install_harnesses or [])]

        existing_row = self.get(user_id, spec.name)
        if existing_row is not None and not update_existing:
            # Create-only: touch nothing, not even a credential — storing one
            # would write a new Credential row on every re-run.
            return existing_row, False, installs

        credential_id: str | None = None
        if spec.credential and spec.credential.token and b.credentialProvider:
            credential = self._credentials.store(
                user_id, CredentialProvider(b.credentialProvider),
                spec.credential.token, is_default=None)
            credential_id = credential.id

        existing = existing_row
        fields = dict(
            userId=user_id,
            providerType=b.providerType,
            harnessNames=list(b.harnessNames),
            credentialProvider=str(b.credentialProvider) if b.credentialProvider else None,
            cliBinary=spec.cliBinary or b.cliBinary,
            harnessDependencies=dict(b.harnessDependencies),
            description=b.description,
            tokenLimits=list(spec.tokenLimits),
        )
        # None leaves an existing row's endpoint as it is (an update that does
        # not mention it must not reset it); a fresh row starts at None.
        if spec.baseUrl is not None or existing is None:
            fields["baseUrl"] = spec.baseUrl
        if existing is None:
            provider = Provider(name=spec.name, createdBy=user_id,
                                modifiedBy=user_id, **fields)
            # A fresh registration keeps whatever credential we just captured
            # and starts available.
            provider.credentialId = credential_id
            created = True
        else:
            for field, value in fields.items():
                setattr(existing, field, value)
            if credential_id is not None:
                existing.credentialId = credential_id
            existing.touch(modified_by=user_id)
            provider = existing
            created = False
        self._repo.save(provider)
        # NB: never put "created" in extra — it is a reserved LogRecord field.
        log.info("provider registered", extra={
            "user_id": user_id, "provider": provider.name,
            "provider_id": provider.id, "newly_created": created})
        return provider, created, installs

    def set_token_limit(self, user_id: str, name: str, *, max_tokens: int,
                        window_seconds: int, replace: bool = False) -> Provider:
        """Attach a self-imposed rolling-window token budget to a provider
        (agent-fallback spec §3.4). A persisted row is required — built-ins
        without a row can't carry limits, so the caller registers first."""
        provider = self.get(user_id, name)
        if provider is None:
            raise ProviderError(
                f"no registered provider named {name!r} — register it first "
                f"with `sprintbaton providers add {name}`")
        limit = SelfImposedLimit(maxTokens=max_tokens, windowSeconds=window_seconds)
        if replace:
            provider.tokenLimits = [limit]
        else:
            provider.tokenLimits.append(limit)
        provider.touch(modified_by=user_id)
        self._repo.save(provider)
        return provider

    def delete(self, user_id: str, name: str) -> bool:
        provider = self.get(user_id, name)
        if provider is None:
            return False
        self._repo.soft_delete(provider.id)
        return True
