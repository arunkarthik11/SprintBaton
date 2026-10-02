"""HarnessRegistry — name -> Harness factory, the same shape as
TaskActionServiceFactory and create_adapter (migration spec §5)."""

from sprintbaton.harness.base import Harness


class HarnessRegistry:
    def __init__(self, harnesses: dict[str, Harness] | None = None):
        self._harnesses: dict[str, Harness] = dict(harnesses or {})

    def register(self, harness: Harness) -> None:
        self._harnesses[harness.name] = harness

    def get(self, name: str) -> Harness:
        # Fail loudly on an unknown name — never default silently.
        if name not in self._harnesses:
            raise KeyError(
                f"unknown harness {name!r} (registered: {sorted(self._harnesses)})"
            )
        return self._harnesses[name]

    def names(self) -> list[str]:
        return sorted(self._harnesses)
