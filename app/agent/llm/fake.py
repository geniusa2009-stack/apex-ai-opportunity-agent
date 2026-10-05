"""Deterministic test doubles. Used by the test-suite and by the mock
end-to-end run; never selected by configuration in production."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from app.agent.errors import AgentError
from app.agent.llm.base import LLMRequest, LLMResponse

Reply = str | dict[str, Any] | AgentError | Callable[[LLMRequest], "str | dict[str, Any]"]


class FakeLLMProvider:
    """Replays scripted replies in order (the last one repeats). A reply
    may be raw text, a dict (sent as JSON), an AgentError (raised) or a
    callable receiving the request."""

    def __init__(self, replies: list[Reply], *, name: str = "fake", model: str = "fake-model"):
        if not replies:
            raise ValueError("FakeLLMProvider needs at least one reply")
        self.name = name
        self.model = model
        self._replies = list(replies)
        self.requests: list[LLMRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        reply = self._replies[min(len(self.requests) - 1, len(self._replies) - 1)]
        if callable(reply) and not isinstance(reply, AgentError):
            reply = reply(request)
        if isinstance(reply, AgentError):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return LLMResponse(text=text, provider=self.name, model=self.model)


class HashingEmbeddingProvider:
    """Deterministic bag-of-words hashing embedder for tests (NOT semantic)."""

    name = "hashing"
    model = "hashing-test"

    def __init__(self, dimension: int = 64):
        self.dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in text.lower().split():
                index = int(hashlib.sha256(token.encode()).hexdigest(), 16) % self.dimension
                vector[index] += 1.0
            out.append(vector)
        return out
