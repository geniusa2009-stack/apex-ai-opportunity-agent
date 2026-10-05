"""LLM abstraction for the AI Opportunity Agent.

`build_llm_client()` turns configuration into an `LLMClient` whose provider
is an `LLMRouter` over the free-only chain AGENT_LLM_CHAIN (or the legacy
AGENT_LLM_PROVIDER/MODEL + AGENT_LLM_FALLBACK_* pair). Swapping providers is
a configuration change; every provider is checked against the free-only
policy when it is built.
"""

from __future__ import annotations

import dataclasses
from typing import Any, TypeVar

from pydantic import BaseModel

from app.agent.errors import ConfigurationError, CostPolicyError
from app.agent.llm.base import (
    ChatMessage,
    EmbeddingProvider,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)
from app.agent.llm.router import LLMRouter, RouterPolicy
from app.agent.llm.structured import StructuredResult, generate_structured
from app.agent.observability import log_stage
from app.core.config import Settings
from app.core.config import settings as global_settings

T = TypeVar("T", bound=BaseModel)

__all__ = [
    "ChatMessage",
    "EmbeddingProvider",
    "LLMClient",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMRouter",
    "RouterPolicy",
    "StructuredResult",
    "build_embedding_provider",
    "build_llm_client",
    "build_provider",
    "build_router",
]


SUPPORTED_CHAIN_PROVIDERS = frozenset({"gemini", "groq", "cloudflare", "openrouter", "ollama"})


def build_provider(name: str | None, model: str | None, cfg: Settings) -> LLMProvider | None:
    """Build ONE provider, enforcing the free-only policy:

    * gemini: the model must be on AGENT_GEMINI_FREE_MODELS (free tier);
    * groq / cloudflare: free plan confirmed by the operator + model checked
      against the provider's own catalog at call time (fail closed);
    * openrouter: explicit ':free' id, verified zero-priced at call time;
    * ollama: the base URL must be local/private.
    """
    from app.agent.llm.providers import (
        CloudflareWorkersAIProvider,
        GeminiProvider,
        GroqProvider,
        OllamaProvider,
        OpenRouterProvider,
    )

    name = (name or "none").lower()
    if name == "none":
        return None
    if name == "gemini":
        if model and model not in cfg.agent_gemini_free_model_set:
            raise CostPolicyError(
                f"gemini model {model!r} is not on AGENT_GEMINI_FREE_MODELS (free-tier allowlist); refused",
                code="LLM_PAID_MODEL_BLOCKED",
            )
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
    if name == "groq":
        return GroqProvider(
            api_key=cfg.groq_api_key,
            model=model,
            free_tier_confirmed=cfg.groq_free_tier_confirmed,
            base_url=cfg.groq_base_url,
            timeout_seconds=cfg.agent_llm_timeout_seconds,
        )
    if name == "cloudflare":
        return CloudflareWorkersAIProvider(
            api_token=cfg.cloudflare_api_token,
            account_id=cfg.cloudflare_account_id,
            model=model,
            free_plan_confirmed=cfg.cloudflare_workers_free_plan_confirmed,
            timeout_seconds=cfg.agent_llm_timeout_seconds,
        )
    if name == "ollama":
        return OllamaProvider(
            model=model,
            base_url=cfg.ollama_base_url,
            api_key=cfg.ollama_api_key,
            timeout_seconds=cfg.agent_llm_timeout_seconds,
        )
    raise ConfigurationError(f"unknown LLM provider {name!r}", code="LLM_UNKNOWN_PROVIDER")


def router_policy(cfg: Settings) -> RouterPolicy:
    return RouterPolicy(
        retries_per_provider=max(0, min(3, cfg.agent_llm_retries_per_provider)),
        max_retry_wait_seconds=cfg.agent_llm_max_retry_wait_seconds,
        cooldown_seconds=cfg.agent_llm_cooldown_seconds,
        disable_seconds=cfg.agent_llm_disable_seconds,
    )


def build_router(cfg: Settings) -> LLMRouter | None:
    providers = [p for p in (build_provider(n, m, cfg) for n, m in cfg.agent_llm_chain_entries) if p is not None]
    if not providers:
        return None
    router = LLMRouter(providers, router_policy(cfg))
    log_stage("llm_chain", chain=router.chain)
    return router


@dataclasses.dataclass
class LLMClient:
    """`primary` is normally an `LLMRouter` (the whole free-only chain);
    `fallback` is kept for callers/tests that pass two plain providers."""

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
                "no LLM provider configured (set AGENT_LLM_CHAIN)", code="LLM_NOT_CONFIGURED"
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
    return LLMClient(
        primary=build_router(cfg),
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
