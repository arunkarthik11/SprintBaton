from enum import StrEnum

from sprintbaton.entities.base import BaseEntity


class ModelProviderType(StrEnum):
    Anthropic = "Anthropic"
    HuggingFace = "HuggingFace"
    OpenAI = "OpenAI"


class Model(BaseEntity):
    """A concrete model, e.g. Claude Opus 4.8."""

    modelType: str = ""  # provider model id, e.g. "claude-opus-4-8"
    modelProviderType: ModelProviderType = ModelProviderType.Anthropic
