"""Optional-dependency imports (pluggable-hosted-backends spec §4.8).

No provider SDK is a base dependency: every module that needs one imports it
through `require_module` at the moment it is used, so `import sprintbaton`
works on an install that carries none of them, and a missing one fails with
an error naming what is missing and the extra that provides it.

Stdlib only, and outside every package with an eager `__init__`, so any
module can import it without creating a cycle.
"""

import importlib
from types import ModuleType


class MissingDependencyError(ImportError):
    """A harness (or backend) was used on an install without its package."""

    def __init__(self, package: str, extra: str, harness: str | None = None,
                 feature: str | None = None, build_arg: str = "PROVIDER_EXTRAS"):
        self.package = package
        self.extra = extra
        self.harness = harness
        self.build_arg = build_arg
        user = f"harness {harness!r}" if harness else (feature or "this feature")
        super().__init__(
            f"{user} needs the {package!r} package, which is not installed. "
            f"Install it with: pip install 'sprintbaton[{extra}]' "
            f"(hosted images: add {extra!r} to the {build_arg} build argument)")


def require_module(module: str, *, package: str, extra: str,
                   harness: str | None = None, feature: str | None = None,
                   build_arg: str = "PROVIDER_EXTRAS") -> ModuleType:
    """Import `module`, or raise MissingDependencyError naming the package and
    the pip extra that provides it. Provider SDKs default to the image's
    PROVIDER_EXTRAS build argument; storage and service clients pass
    STORAGE_EXTRAS (spec §6)."""
    try:
        return importlib.import_module(module)
    except ImportError as e:
        raise MissingDependencyError(package, extra, harness, feature, build_arg) from e


def require_storage_module(module: str, *, package: str, extra: str,
                           feature: str) -> ModuleType:
    """require_module for a storage/service client (spec §5's storage extras)."""
    return require_module(module, package=package, extra=extra, feature=feature,
                          build_arg="STORAGE_EXTRAS")
