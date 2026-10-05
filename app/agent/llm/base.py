"""Provider-agnostic LLM interfaces.

The core never imports a vendor SDK. A provider only has to turn a list of
chat messages into raw text (expected to be JSON). Validation, bounded
self-correction and fallback live in `structured.py`, once, for every
provider.
"""

from __future__ import annotations

import dataclasses
from typing import Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant"]


@dataclasses.dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclasses.dataclass(frozen=True)
class LLMRequest:
    system: str
    messages: list[ChatMessage]
    temperature: float = 0.1
    max_output_tokens: int = 2048
    json_output: bool = True


@dataclasses.dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """Adapters: GeminiProvider, OpenRouterProvider (any OpenAI-compatible
    endpoint), FakeLLMProvider (tests). Implementations must raise
    `app.agent.errors.AgentError` subclasses with the right category."""

    name: str
    model: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    model: str
    dimension: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
