"""Free-only, ordered LLM routing for the Agent.

`LLMRouter` is itself an `LLMProvider`: it tries an ordered chain of
providers (configured by AGENT_LLM_CHAIN) and returns the first answer.

Per provider (one "slot" = provider + model):

* RATE_LIMIT / TRANSIENT / EXTERNAL_SERVICE / SYSTEM (429, 503, timeouts,
  network errors): retried at most `retries_per_provider` times with capped
  exponential backoff that honours the server's retry-after. If the server
  asks for a longer wait than `max_retry_wait_seconds`, the slot is put on
  cooldown for that long and the router moves on -- it never sleeps for
  minutes inside a job.
* AUTHENTICATION (401/402/403, model unavailable/404, cost-policy block,
  configuration): no retry; the slot is disabled for `disable_seconds`
  and the router moves on.
* DATA from a provider (LLM_OUTPUT_TRUNCATED, HTTP 400/413/422): the next
  slot is tried (a different model may fit the budget); no cooldown.

Application-level validation (JSON/Pydantic schema, grounding of links and
dates) happens ABOVE the router in `structured.py`: those failures are
corrected by the bounded self-correction loop and then go to NEEDS_REVIEW;
they never trigger a provider fallback.

If every slot fails, one aggregated error is raised whose category feeds the
existing job runner: retryable if any slot failed transiently (retry with
backoff, then DEAD_LETTER), DATA if slots only rejected the request
(NEEDS_REVIEW),
otherwise AUTHENTICATION (FAILED: an operator must fix the config).

Cost safety is NOT decided here: the chain only ever contains providers
built by `build_router()`, which accepts Gemini models on the free-tier
allowlist, OpenRouter models that pass the zero-price checks, and local
Ollama. The router can only move *down* that list, never to anything else.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable, Sequence

from app.agent.errors import (
    AgentError,
    AuthenticationError,
    DataError,
    ErrorCategory,
    ExternalServiceError,
    RateLimitError,
    classify_exception,
)
from app.agent.llm.base import LLMProvider, LLMRequest, LLMResponse
from app.agent.observability import log_stage

TRUNCATED = "LLM_OUTPUT_TRUNCATED"
_RETRYABLE = frozenset(
    {ErrorCategory.RATE_LIMIT, ErrorCategory.TRANSIENT, ErrorCategory.EXTERNAL_SERVICE, ErrorCategory.SYSTEM}
)


@dataclasses.dataclass(frozen=True)
class RouterPolicy:
    retries_per_provider: int = 1
    max_retry_wait_seconds: float = 20.0
    backoff_base_seconds: float = 2.0
    # After a slot exhausts its retries on a transient error.
    cooldown_seconds: float = 60.0
    # After an auth / model-unavailable / cost-policy error.
    disable_seconds: float = 3600.0


@dataclasses.dataclass
class _Cooldown:
    until: float
    category: ErrorCategory
    code: str


class CooldownRegistry:
    """Per-process memory of slots that should be skipped for a while, so a
    rate-limited model is not hammered by every job in the same minute."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._entries: dict[str, _Cooldown] = {}

    def get(self, key: str) -> _Cooldown | None:
        entry = self._entries.get(key)
        if entry and entry.until > self._clock():
            return entry
        self._entries.pop(key, None)
        return None

    def put(self, key: str, seconds: float, error: AgentError) -> None:
        self._entries[key] = _Cooldown(self._clock() + max(0.0, seconds), error.category, error.code)

    def remaining(self, key: str) -> float:
        entry = self.get(key)
        return max(0.0, entry.until - self._clock()) if entry else 0.0

    def clear(self) -> None:
        self._entries.clear()


DEFAULT_COOLDOWNS = CooldownRegistry()


def slot_key(provider: LLMProvider) -> str:
    return f"{provider.name}:{provider.model}"


class LLMRouter:
    name = "router"

    def __init__(
        self,
        providers: Sequence[LLMProvider],
        policy: RouterPolicy | None = None,
        *,
        cooldowns: CooldownRegistry | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if not providers:
            raise ValueError("LLMRouter needs at least one provider")
        self.providers = list(providers)
        self.policy = policy or RouterPolicy()
        self.cooldowns = cooldowns if cooldowns is not None else DEFAULT_COOLDOWNS
        self._sleep = sleep
        # LLMProvider protocol; the model that actually answered is on each response.
        self.model = self.providers[0].model

    @property
    def chain(self) -> list[str]:
        return [slot_key(p) for p in self.providers]

    def _backoff(self, attempt: int, error: AgentError) -> float:
        if error.retry_after is not None:
            return float(error.retry_after)
        return self.policy.backoff_base_seconds * (2 ** (attempt - 1))

    async def complete(self, request: LLMRequest) -> LLMResponse:
        failures: list[AgentError] = []
        for position, provider in enumerate(self.providers):
            key = slot_key(provider)
            cooling = self.cooldowns.get(key)
            if cooling is not None:
                log_stage("llm_route_skip", provider=provider.name, model=provider.model, error_code=cooling.code)
                failures.append(AgentError(f"{key} cooling down", code=cooling.code, category=cooling.category,
                                           retry_after=self.cooldowns.remaining(key)))
                continue
            attempts = 1 + max(0, self.policy.retries_per_provider)
            for attempt in range(1, attempts + 1):
                try:
                    response = await provider.complete(request)
                except Exception as raw:  # noqa: BLE001 - classified below
                    error = classify_exception(raw)
                else:
                    log_stage("llm_route_ok", provider=response.provider, model=response.model,
                              position=position, attempt=attempt)
                    return response

                log_stage("llm_route_error", level=logging.WARNING, provider=provider.name, model=provider.model,
                          position=position, attempt=attempt, error_code=error.code, category=error.category.value)
                if error.category in (ErrorCategory.VALIDATION, ErrorCategory.DATA):
                    # This provider could not serve THIS request (truncated
                    # output, HTTP 400/413/422): the next model may. No
                    # cooldown -- the provider itself is fine.
                    failures.append(error)
                    break
                if error.category not in _RETRYABLE:  # AUTHENTICATION incl. cost policy / model gone
                    self.cooldowns.put(key, self.policy.disable_seconds, error)
                    failures.append(error)
                    break
                wait = self._backoff(attempt, error)
                if attempt >= attempts or wait > self.policy.max_retry_wait_seconds:
                    cooldown = max(self.policy.cooldown_seconds, float(error.retry_after or 0))
                    self.cooldowns.put(key, cooldown, error)
                    failures.append(error)
                    break
                await self._sleep(wait)
        raise self._aggregate(failures)

    def _aggregate(self, failures: list[AgentError]) -> AgentError:
        codes = ",".join(f.code for f in failures) or "none"
        message = f"all {len(self.providers)} LLM provider(s) failed ({codes})"
        retryable = [f for f in failures if f.category in _RETRYABLE]
        if retryable:
            waits = [f.retry_after for f in retryable if f.retry_after]
            cls = RateLimitError if any(f.category == ErrorCategory.RATE_LIMIT for f in retryable) else ExternalServiceError
            return cls(message, code="LLM_ALL_PROVIDERS_FAILED", retry_after=min(waits) if waits else None)
        data = [f for f in failures if f.category in (ErrorCategory.VALIDATION, ErrorCategory.DATA)]
        if data:
            code = TRUNCATED if all(f.code == TRUNCATED for f in data) else "LLM_ALL_PROVIDERS_REJECTED_REQUEST"
            return DataError(message, code=code)
        return AuthenticationError(message, code="LLM_ALL_PROVIDERS_FAILED")
