"""Startup dependency check (pluggable-hosted-backends spec §4.9).

Walks every harness reachable from a bound action — each fallback-chain entry,
and each execution tier's chain — and checks that the packages it needs are
importable, using the same per-harness metadata `providers add` installs from
(`BuiltinProvider.harnessDependencies`). The point is to turn a mis-built
custom image into a startup failure rather than a per-task runtime failure.

Like `serve`'s unbound-action warning, it checks the process's own user: in
hosted mode each tenant binds their own agents, so this covers the
deployment-wide bindings, not every tenant's.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from typing import Callable

from sprintbaton.entities.agent_definition import AgentDefinition
from sprintbaton.entities.provider import HarnessDependency
from sprintbaton.extras import PROVIDER_PACKAGES
from sprintbaton.providers.registry import BUILTIN_PROVIDERS
from sprintbaton.storage.base import EntityDAO


def harness_dependency(harness_name: str) -> HarnessDependency | None:
    """What a harness needs installed, whichever provider it runs under — the
    import is a property of the harness, so a custom provider on
    `single_shot` needs `anthropic` exactly as the built-in does."""
    for provider in BUILTIN_PROVIDERS.values():
        dep = provider.harnessDependencies.get(harness_name)
        if dep is not None:
            return dep
    return None


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # a missing parent package
        return False


@dataclass
class MissingEntry:
    agent: str
    harness: str
    package: str
    extra: str


@dataclass
class ChainReport:
    """One bound chain: an action (or an execution tier) and its entries."""

    label: str                       # "classification", "execution E3"
    agents: list[str]
    missing: list[MissingEntry] = field(default_factory=list)

    @property
    def satisfiable(self) -> bool:
        """At least one entry can run on this install."""
        return len(self.missing) < len(self.agents)


def check_bound_dependencies(
        settings, definitions: EntityDAO[AgentDefinition], user_id: str,
        importable: Callable[[str], bool] | None = None) -> list[ChainReport]:
    """Every bound chain with at least one entry whose harness dependency is
    missing. Unbound actions and unknown definition names are reported
    elsewhere (the unbound-action warning, the resolver) and skipped here."""
    from sprintbaton.models.definitions import (
        _EXECUTION_TIERS,
        ACTION_PROMPTS,
        _split_chain,
        parse_tier_agent_map,
    )

    importable = importable or _importable
    chains: list[tuple[str, list[str]]] = []
    for action in ACTION_PROMPTS:
        if action == "execution":
            continue
        chains.append((action, _split_chain(getattr(settings, f"sprintbaton_{action}_agent", ""))))
    try:
        tier_map = parse_tier_agent_map(settings.sprintbaton_execution_tier_agents)
    except ValueError:
        tier_map = {}  # malformed config fails loudly at resolution
    for tier in _EXECUTION_TIERS:
        names = (_split_chain(tier_map[tier], sep="+") if tier in tier_map
                 else _split_chain(settings.sprintbaton_execution_agent))
        chains.append((f"execution {tier.value}", names))

    reports = []
    for label, names in chains:
        report = ChainReport(label=label, agents=[])
        for name in names:
            definition = definitions.find_one({"name": name, "userId": user_id})
            if definition is None:
                continue
            report.agents.append(name)
            dep = harness_dependency(definition.harnessName)
            if dep is None or dep.importCheckModule is None:
                continue
            if not importable(dep.importCheckModule):
                extra = dep.pipExtra or dep.importCheckModule
                report.missing.append(MissingEntry(
                    agent=name, harness=definition.harnessName,
                    package=PROVIDER_PACKAGES.get(extra, dep.importCheckModule),
                    extra=extra))
        if report.missing:
            reports.append(report)
    return reports


def describe(report: ChainReport, hosted: bool) -> list[str]:
    """Human-readable lines for one report, naming the remedy."""
    lines = []
    for entry in report.missing:
        remedy = (f"add {entry.extra!r} to the image's PROVIDER_EXTRAS build argument"
                  if hosted else f"pip install 'sprintbaton[{entry.extra}]'")
        lines.append(
            f"action {report.label}: agent {entry.agent!r} runs on harness "
            f"{entry.harness!r}, which needs the {entry.package!r} package "
            f"(extra {entry.extra!r}) — not installed; {remedy}")
    if report.satisfiable:
        lines.append(f"  (action {report.label} still has a usable fallback-chain entry)")
    return lines
