"""The `.sprintbaton/` structural contract, as pure functions over a directory
(docs/project-initialization-task-spec.md §8.5; the contract itself is
docs/metadata-modelling.md, restated in the metadata_generation prompt).

An in-place-editing agent can leave its directory in any shape, so every init
pass is validated before its revision is published: violations bounce the run
back to the agent with the list as its situation report, and a pass that still
violates after its budget fails. Only *structure* is checked — content
accuracy and skeleton headings remain the prompt's job.

Unit-testable without a model: every function here takes a path and returns a
ContractReport.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from sprintbaton.metadata.format import METADATA_FORMAT_VERSION

INFO_FILE = "info"
INFO_FIRST_LINE = f"version: {METADATA_FORMAT_VERSION}"

REPO_TOP_LEVEL = frozenset({INFO_FILE, "UI", "Backend", "Product"})
REPO_SUBDIRS: dict[str, frozenset[str]] = {
    "UI": frozenset({"Screens", "Components", "Test"}),
    "Backend": frozenset({"Services", "Entities", "API", "ML", "Test", "AdditionalInfo"}),
}
PROJECT_TOP_LEVEL = frozenset({INFO_FILE, "repos"})

_KEBAB_STEM = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class ContractReport:
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def render(self) -> str:
        """The situation report a bounced run receives."""
        lines = ["The metadata directory violates the contract:"]
        lines += [f"- {v}" for v in self.violations]
        return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _check_info(root: Path, report: ContractReport) -> str:
    info = root / INFO_FILE
    if not info.is_file():
        report.violations.append(f"missing the `{INFO_FILE}` index file")
        return ""
    text = info.read_text(errors="replace")
    first = text.splitlines()[0] if text else ""
    if first != INFO_FIRST_LINE:
        report.violations.append(
            f"`{INFO_FILE}` must start with the exact line `{INFO_FIRST_LINE}` "
            f"(found {first!r})")
    return text


def _check_md_file(path: Path, root: Path, report: ContractReport) -> None:
    rel = _rel(path, root)
    if path.suffix != ".md":
        report.violations.append(f"`{rel}` must be a `.md` file")
        return
    if not _KEBAB_STEM.match(path.stem):
        report.violations.append(
            f"`{rel}` must have a kebab-case name (lowercase letters, digits, "
            f"single hyphens)")


def _check_leaf_dir(directory: Path, root: Path, report: ContractReport) -> list[Path]:
    """A directory that may hold only `.md` files. Returns those files."""
    files: list[Path] = []
    entries = sorted(directory.iterdir())
    if not entries:
        report.violations.append(f"`{_rel(directory, root)}/` is an empty directory")
    for entry in entries:
        if entry.is_dir():
            report.violations.append(
                f"`{_rel(entry, root)}/` is not allowed — "
                f"`{_rel(directory, root)}/` holds files only")
            continue
        _check_md_file(entry, root, report)
        files.append(entry)
    return files


def validate_repo_metadata(root: Path | str) -> ContractReport:
    """A repository's `.sprintbaton/` (spec §8.5, per repo)."""
    root = Path(root)
    report = ContractReport()
    if not root.is_dir():
        report.violations.append("the metadata directory does not exist")
        return report
    info_text = _check_info(root, report)
    documented: list[Path] = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        if name == INFO_FILE:
            continue
        if name == "tasks":
            report.violations.append(
                "`tasks` must not appear in published metadata (it is the "
                "per-task workspace, never part of a revision)")
            continue
        if name not in REPO_TOP_LEVEL:
            report.violations.append(
                f"`{name}` is not allowed at the top level (allowed: "
                f"{', '.join(sorted(REPO_TOP_LEVEL))})")
            continue
        if not entry.is_dir():
            report.violations.append(f"`{name}` must be a directory")
            continue
        children = sorted(entry.iterdir())
        if not children:
            report.violations.append(f"`{name}/` is an empty directory")
            continue
        if name == "Product":
            documented += _check_leaf_dir(entry, root, report)
            continue
        allowed = REPO_SUBDIRS[name]
        for child in children:
            rel = _rel(child, root)
            if child.name not in allowed or not child.is_dir():
                report.violations.append(
                    f"`{rel}` is not allowed (`{name}/` may contain only the "
                    f"directories {', '.join(sorted(allowed))})")
                continue
            documented += _check_leaf_dir(child, root, report)
    for path in documented:
        if path.name not in info_text:
            report.warnings.append(
                f"`{_rel(path, root)}` is not listed in the `{INFO_FILE}` index")
    return report


def validate_project_metadata(root: Path | str,
                              expected_repo_ids: list[str]) -> ContractReport:
    """The combined project index (spec §8.5, project): `info` plus exactly one
    `repos/<repoId>.md` per member repo that has a published revision."""
    root = Path(root)
    report = ContractReport()
    if not root.is_dir():
        report.violations.append("the project metadata directory does not exist")
        return report
    _check_info(root, report)
    for entry in sorted(root.iterdir()):
        if entry.name not in PROJECT_TOP_LEVEL:
            report.violations.append(
                f"`{entry.name}` is not allowed (allowed: "
                f"{', '.join(sorted(PROJECT_TOP_LEVEL))})")
        elif entry.name == "repos" and not entry.is_dir():
            report.violations.append("`repos` must be a directory")
    repos_dir = root / "repos"
    expected = {f"{repo_id}.md" for repo_id in expected_repo_ids}
    present: set[str] = set()
    if repos_dir.is_dir():
        for entry in sorted(repos_dir.iterdir()):
            if entry.is_dir():
                report.violations.append(
                    f"`repos/{entry.name}/` is not allowed — `repos/` holds files only")
                continue
            present.add(entry.name)
    for name in sorted(expected - present):
        report.violations.append(f"missing `repos/{name}`")
    for name in sorted(present - expected):
        report.violations.append(
            f"`repos/{name}` does not match a member repository with published "
            f"metadata — remove it via removedFiles")
    return report
