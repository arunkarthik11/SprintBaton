"""Versioned prompt templates.

AgentPrompt follows a decorator/chain pattern (docs/entities.md): prompts can be
chained so that e.g. the MetadataPrompt (how to consult .sprintbaton metadata)
prefixes every TaskActionPrompt. Templates live in prompts/templates/*.md and
are plain-text placeholders the user tunes later; `{placeholders}` are filled
with str.format.

Three layers compose, in render order (agent-system-prompt spec §4.4):

    metadata.md  ->  the agent's system preamble  ->  the action's template

The preamble is `AgentDefinition.systemPromptName` — model-specific standing
instructions, reusable across every action the agent is wired to. It sits
*between* metadata.md and the action template rather than at the very top so
that (a) metadata.md stays one byte-identical, shared cacheable prefix for
every role and agent, and (b) behavior instructions land nearest the task
instructions they modify.

A preamble is rendered **literally** (§4.5): it carries no task placeholders,
and the most natural thing to put in one is a literal JSON example, which
`str.format` would raise KeyError on.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent / "templates"
PROMPT_VERSION = "v1"


class AgentPrompt:
    def __init__(self, prompt_id: str, template: str,
                 parent: AgentPrompt | None = None, literal: bool = False):
        self.prompt_id = prompt_id
        self.template = template
        self.parent = parent  # chained prompt rendered before this one
        # literal=True -> emit the template verbatim, never str.format-ed
        # (agent-system-prompt spec §4.5).
        self.literal = literal

    def chain(self, parent: AgentPrompt) -> AgentPrompt:
        return AgentPrompt(self.prompt_id, self.template, parent=parent,
                           literal=self.literal)

    def render(self, **kwargs) -> str:
        parts = []
        if self.parent is not None:
            parts.append(self.parent.render(**kwargs))
        parts.append(self.template if self.literal
                     else self.template.format(**kwargs))
        return "\n\n".join(parts)


class PromptRegistry:
    """Loads versioned templates from disk; keyed by name."""

    def __init__(self, template_dir: Path = TEMPLATE_DIR, version: str = PROMPT_VERSION):
        self._dir = template_dir
        self._version = version
        self._cache: dict[str, AgentPrompt] = {}
        # System preambles are cached separately: same file, different render
        # mode (literal), so one cache entry could not serve both.
        self._system_cache: dict[str, AgentPrompt] = {}

    def path_for(self, name: str) -> Path:
        return self._dir / f"{name}.md"

    def exists(self, name: str) -> bool:
        """Is there a template by this name? Lets the write surfaces reject an
        unknown name at creation time instead of once per task inside the
        dispatch path (agent-system-prompt spec §5)."""
        return self.path_for(name).is_file()

    def get(self, name: str) -> AgentPrompt:
        if name not in self._cache:
            self._cache[name] = AgentPrompt(
                prompt_id=f"{name}@{self._version}",
                template=self.path_for(name).read_text(),
            )
        return self._cache[name]

    def system_prompt(self, name: str) -> AgentPrompt:
        """An agent's system preamble: the same file `get` would read, rendered
        literally (§4.5)."""
        if name not in self._system_cache:
            self._system_cache[name] = AgentPrompt(
                prompt_id=f"{name}@{self._version}",
                template=self.path_for(name).read_text(),
                literal=True,
            )
        return self._system_cache[name]

    def compose(self, action_name: str, *, system: str | None = None,
                with_metadata: bool = True) -> AgentPrompt:
        """metadata -> system -> action, omitting either absent layer.

        The composed prompt_id records every layer that shaped the outcome
        (§4.6), since TaskActionEvent.promptId is the analytics handle on
        "which prompt produced this": `review@v1` alone, `review@v1+strict@v1`
        with a preamble. The metadata layer stays invisible — it is a constant
        across every role and agent.
        """
        action = self.get(action_name)
        parent: AgentPrompt | None = self.get("metadata") if with_metadata else None
        prompt_id = action.prompt_id
        if system is not None:
            preamble = self.system_prompt(system)
            parent = preamble.chain(parent) if parent is not None else preamble
            prompt_id = f"{action.prompt_id}+{preamble.prompt_id}"
        return AgentPrompt(prompt_id, action.template, parent=parent)

    def action_prompt(self, name: str, system: str | None = None) -> AgentPrompt:
        """A TaskActionPrompt chained after the MetadataPrompt, optionally
        behind the agent's own system preamble."""
        return self.compose(name, system=system, with_metadata=True)
