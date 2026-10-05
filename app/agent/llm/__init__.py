"""LLM abstraction for the AI Opportunity Agent.

`build_llm_client()` turns configuration into an `LLMClient` (primary +
optional fallback provider). Swapping providers is a configuration change:
AGENT_LLM_PROVIDER / AGENT_LLM_MODEL (+ the provider's key).
"""

from __future__ import annotations

import dataclasses
from typing import Any, TypeVar

from pydantic import BaseModel

from app.agent.errors import ConfigurationError
from app.agent.llm.base import ChatMessage, EmbeddingProvider, LLMProvider, LLMRequest, LLMResponse
from app.agent.llm.structured import StructuredResult, generate_structured
from app.core.config import Settings, settings as global_settings

T = TypeVar("T", bound=BaseModel)

__all__ = [
    "ChatMessage",
    "EmbeddingProvider",
    "LLMClient",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "StructuredResult",
    "build_embedding_provider",
    "build_llm_client",
    "build_provider",
]


def build_provider(name: str | None, model: str | None, cfg: Settings) -> LLMProvider | None:
    from app.agent.llm.providers import GeminiProvider, OpenRouterProvider

    name = (name or "none").lower()
    if name == "none":
        return None
    if name == "gemini":
        return GeminiProvider(api_key=cfg.gemini_api_key, model=model, timeout_seconds=cfg.agent_llm_timeout_seconds)
    if name == "openrouter":
        return OpenRouterProvider(
            api_key=cfg.openrouter_api_key,
            model=model,
            base_url=cfg.openrouter_base_url,
            app_title=cfg.openrouter_app_title,
            http_referer=cfg.openrouter_http_referer,
            timeout_seconds=cfg.agent_llm_timeout_seconds,
        )
    raise ConfigurationError(f"unknown LLM provider {name!r}", code="LLM_UNKNOWN_PROVIDER")


@dataclasses.dataclass
class LLMClient:
    primary: LLMProvider | None
    fallback: LLMProvider | None = None
    max_validation_retries: int = 2
    temperature: float = 0.1

    @property
    def available(self) -> bool:
        return self.primary is not None

    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        stage: str,
        validation_context: dict[str, Any] | None = None,
        max_output_tokens: int = 2048,
    ) -> StructuredResult[T]:
        if self.primary is None:
            raise ConfigurationError(
                "no LLM provider configured (set AGENT_LLM_PROVIDER and AGENT_LLM_MODEL)", code="LLM_NOT_CONFIGURED"
            )
        return await generate_structured(
            self.primary,
            system=system,
            user=user,
            schema=schema,
            max_validation_retries=self.max_validation_retries,
            temperature=self.temperature,
            max_output_tokens=max_output_tokens,
            validation_context=validation_context,
            stage=stage,
            fallback=self.fallback,
        )


def build_llm_client(cfg: Settings | None = None) -> LLMClient:
    cfg = cfg or global_settings
    primary = build_provider(cfg.agent_llm_provider, cfg.agent_llm_model, cfg)
    fallback = (
        build_provider(cfg.agent_llm_fallback_provider, cfg.agent_llm_fallback_model, cfg)
        if cfg.agent_llm_fallback_provider
        else None
    )
    return LLMClient(
        primary=primary,
        fallback=fallback,
        max_validation_retries=cfg.agent_llm_max_validation_retries,
        temperature=cfg.agent_llm_temperature,
    )


def build_embedding_provider(cfg: Settings | None = None) -> EmbeddingProvider | None:
    from app.agent.llm.providers import GeminiEmbeddingProvider, OpenAIEmbeddingProvider

    cfg = cfg or global_settings
    name = (cfg.agent_embedding_provider or "none").lower()
    if name == "none":
        return None
    if name == "openai":
        return OpenAIEmbeddingProvider(
            api_key=cfg.openai_api_key, model=cfg.agent_embedding_model, dimension=cfg.embedding_dimension
        )
    if name == "gemini":
        return GeminiEmbeddingProvider(
            api_key=cfg.gemini_api_key, model=cfg.agent_embedding_model, dimension=cfg.embedding_dimension
        )
    raise ConfigurationError(f"unknown embedding provider {name!r}", code="EMBEDDING_UNKNOWN_PROVIDER")
