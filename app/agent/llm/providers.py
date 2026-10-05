"""LLM / embedding provider adapters.

* `GeminiProvider` -- google-genai SDK (already a backend dependency),
  same call shape the existing aggregator uses.
* `OpenRouterProvider` -- OpenRouter, restricted to zero-priced models.
* `OllamaProvider` -- a local OpenAI-compatible server (Ollama, LM Studio,
  llama.cpp); refuses non-local URLs.
* `OpenAIEmbeddingProvider`, `GeminiEmbeddingProvider` -- embeddings,
  with a runtime check that every returned vector has the configured size.

Model names always come from configuration; nothing here assumes one.
Every error is mapped to an `AgentError` category so the job runner can
decide between retry / dead letter / human review.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.agent.errors import (
    AgentError,
    ConfigurationError,
    CostPolicyError,
    DataError,
    ErrorCategory,
    ExternalServiceError,
    TransientError,
    category_for_http_status,
)
from app.agent.llm.base import LLMRequest, LLMResponse

_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s")


# Known provider-specific 4xx cases, matched on the error body. Everything
# else that is a 400/413/422 is treated as an INVALID REQUEST: it fails
# clearly (job -> NEEDS_REVIEW) instead of being hidden by another provider.
_INVALID_KEY = re.compile(r"API_KEY_INVALID|API key not valid|invalid api key|No auth credentials", re.IGNORECASE)
_REGION_BLOCKED = re.compile(r"location is not supported|not available in your (?:country|region)", re.IGNORECASE)
_MODEL_GONE = re.compile(
    r"is not found for API version|not supported for generateContent|no longer available|"
    r"No endpoints found|is not a valid model ID|model.{0,40}does not exist",
    re.IGNORECASE,
)
_CONTEXT_TOO_LONG = re.compile(r"context length|context window|maximum context|too many tokens|input token count", re.IGNORECASE)


def http_error(prefix: str, status: int, *, retry_after: float | None = None, detail: str = "",
               body: str = "") -> AgentError:
    """Map an HTTP failure to an AgentError.

    * 404, and 400s whose body says the model is gone / has no endpoint ->
      LLM_MODEL_UNAVAILABLE (provider problem: the router moves on).
    * 400 "API key not valid" (Gemini answers 400, not 401) -> auth.
    * 400 "location is not supported" -> provider unavailable here.
    * 400 context-length exceeded -> LLM_CONTEXT_TOO_LONG (model-specific
      limit: another model may accept it).
    * any other 400/413/422 -> LLM_INVALID_REQUEST (DATA): our request is
      wrong; no fallback, a human looks at it.
    * 401/402/403 auth, 429 rate limit, 5xx external service: unchanged.
    """
    from app.agent.observability import redact

    text = f"{detail} {body}"
    snippet = f": {redact(body.strip())[:160]}" if body.strip() and not detail else detail
    if status == 404 or (status == 400 and _MODEL_GONE.search(text)):
        return AgentError(f"{prefix}: model not available (HTTP {status}){snippet}", code="LLM_MODEL_UNAVAILABLE",
                          category=ErrorCategory.AUTHENTICATION)
    if status == 400 and _INVALID_KEY.search(text):
        return AgentError(f"{prefix}: API key rejected (HTTP 400)", code="LLM_AUTH_INVALID",
                          category=ErrorCategory.AUTHENTICATION)
    if status == 400 and _REGION_BLOCKED.search(text):
        return AgentError(f"{prefix}: not available in this region (HTTP 400)", code="LLM_PROVIDER_UNAVAILABLE",
                          category=ErrorCategory.AUTHENTICATION)
    if status in (400, 413) and _CONTEXT_TOO_LONG.search(text):
        return DataError(f"{prefix}: input exceeds this model's context (HTTP {status})", code="LLM_CONTEXT_TOO_LONG")
    if status in (400, 413, 422):
        return DataError(f"{prefix}: invalid request (HTTP {status}){snippet}", code="LLM_INVALID_REQUEST")
    return AgentError(f"{prefix} HTTP {status}{snippet}", code=f"{prefix.upper()}_HTTP_{status}",
                      category=category_for_http_status(status), retry_after=retry_after)


def _error_message(response: httpx.Response) -> str:
    """The provider's error message (never request headers, so never a key)."""
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            raw = error.get("metadata", {}).get("raw") if isinstance(error.get("metadata"), dict) else None
            return f"{error.get('message', '')} {raw or ''}"[:500]
        return str(error or "")[:500]
    except ValueError:
        return response.text[:500]


def _retry_after_header(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "")
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _require(value: str | None, setting: str) -> str:
    if not value:
        raise ConfigurationError(f"{setting} is not configured", code="LLM_NOT_CONFIGURED")
    return value


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------

class GeminiProvider:
    name = "gemini"

    def __init__(self, *, api_key: str | None, model: str | None, timeout_seconds: float = 60.0, client: Any = None):
        self.model = _require(model, "AGENT_LLM_MODEL (gemini)")
        self._api_key = _require(api_key, "GEMINI_API_KEY")
        self._timeout = timeout_seconds
        self._client = client

    def _get_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        from google.genai import errors as genai_errors
        from google.genai import types as genai_types

        contents = [
            {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
            for m in request.messages
        ]
        config = genai_types.GenerateContentConfig(
            system_instruction=request.system,
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
            response_mime_type="application/json" if request.json_output else None,
        )
        try:
            response = await asyncio.wait_for(
                self._get_client().aio.models.generate_content(model=self.model, contents=contents, config=config),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            raise TransientError("gemini request timed out", code="LLM_TIMEOUT") from exc
        except genai_errors.APIError as exc:
            status = int(getattr(exc, "code", None) or 500)
            match = _RETRY_DELAY.search(str(getattr(exc, "details", "") or exc))
            raise http_error("gemini", status, retry_after=float(match.group(1)) if match else None,
                             body=str(getattr(exc, "message", "") or "")[:500]) from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TransientError(f"gemini network error: {type(exc).__name__}", code="LLM_NETWORK_ERROR") from exc
        candidates = getattr(response, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", None) if candidates else None
        if finish is not None and str(getattr(finish, "name", finish)).upper().endswith("MAX_TOKENS"):
            # Partial output must never be treated as an answer. Note: on
            # "thinking" models the hidden reasoning tokens count against
            # max_output_tokens too (seen live: 61 thought tokens exhausted a
            # 64-token budget), so a too-small budget truncates the reply.
            raise DataError(
                "gemini output was truncated at max_output_tokens", code="LLM_OUTPUT_TRUNCATED"
            )
        text = getattr(response, "text", None)
        if not text:
            raise ExternalServiceError("gemini returned no content (possibly blocked)", code="LLM_EMPTY_RESPONSE")
        usage = getattr(response, "usage_metadata", None)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
        )


# --------------------------------------------------------------------------
# OpenAI-compatible: OpenRouter (free models only) and local Ollama
# --------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# OpenRouter "max_price" (USD per million tokens / per request / per image).
# Documented as a HARD limit: a request that cannot be served at this price
# is refused instead of routed to a paid endpoint.
_ZERO_PRICE = {"prompt": 0, "completion": 0, "request": 0, "image": 0}
_FREE_CATALOG_TTL_SECONDS = 3600


class OpenAICompatibleProvider:
    """Any `/chat/completions` server. Subclasses decide the cost policy."""

    name = "openai_compatible"

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str | None,
        base_url: str,
        app_title: str | None = None,
        http_referer: str | None = None,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = _require(model, f"model for {self.name}")
        self._api_key = api_key or ""
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport
        self._headers = {"Content-Type": "application/json"}
        if self._api_key:
            self._headers["Authorization"] = f"Bearer {self._api_key}"
        if app_title:
            self._headers["X-OpenRouter-Title"] = app_title
            self._headers["X-Title"] = app_title  # older header name, still accepted
        if http_referer:
            self._headers["HTTP-Referer"] = http_referer

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        async with self._client() as client:
            return await client.post(f"{self._base_url}/chat/completions", json=body, headers=self._headers)

    async def _before_request(self) -> None:
        """Cost-policy hook (OpenRouter verifies the model is free)."""

    def _extra_body(self) -> dict[str, Any]:
        return {}

    def _after_response(self, payload: dict[str, Any]) -> None:
        """Cost-policy hook run on every successful response."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        await self._before_request()
        messages = [{"role": "system", "content": request.system}] + [
            {"role": m.role, "content": m.content} for m in request.messages
        ]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            **self._extra_body(),
        }
        if request.json_output:
            body["response_format"] = {"type": "json_object"}
        try:
            response = await self._post(body)
            if response.status_code == 400 and "response_format" in response.text and "response_format" in body:
                # Some models do not support JSON mode; the prompt still asks
                # for JSON and the validator still enforces it.
                body.pop("response_format")
                response = await self._post(body)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TransientError(f"{self.name} network error: {type(exc).__name__}", code="LLM_NETWORK_ERROR") from exc

        if response.status_code >= 400:
            raise http_error(self.name, response.status_code, retry_after=_retry_after_header(response),
                             body=_error_message(response))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalServiceError(f"{self.name} returned non-JSON body", code="LLM_BAD_RESPONSE") from exc
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"] or {}
            raw_code = str(error.get("code", ""))
            status = int(raw_code) if raw_code.isdigit() else 502
            raise http_error(self.name, status, detail=f": {str(error.get('message', ''))[:200]}")
        self._after_response(payload)
        try:
            choice = payload["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExternalServiceError(f"{self.name} response has no choices", code="LLM_BAD_RESPONSE") from exc
        if choice.get("finish_reason") == "length":
            raise DataError(f"{self.name} output was truncated at max_tokens", code="LLM_OUTPUT_TRUNCATED")
        if not text:
            raise ExternalServiceError(f"{self.name} returned empty content", code="LLM_EMPTY_RESPONSE")
        usage = payload.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=str(payload.get("model") or self.model),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter, FREE models only. Four independent guards:

    0. The model id must be an explicit ``:free`` route. Some catalog entries
       show prompt/completion = 0 without being a free route (e.g. preview or
       audio models billed in units the catalog does not list); those are
       refused outright.

    1. Before the first call (and hourly) the public model catalog is read and
       the configured model must have ALL prices equal to 0. If the catalog
       cannot be read, the provider refuses to run (fail closed).
    2. Every request carries `provider.max_price = 0` -- OpenRouter refuses to
       route it to any endpoint that would charge.
    3. Every response's reported `usage.cost` is checked; anything above 0
       disables this provider for the process and is logged as an error.
    """

    name = "openrouter"

    def __init__(self, *, api_key: str | None, model: str | None,
                 base_url: str = "https://openrouter.ai/api/v1", clock=time.monotonic, **kwargs: Any):
        _require(api_key, "OPENROUTER_API_KEY")
        super().__init__(api_key=api_key, model=model, base_url=base_url, **kwargs)
        if not is_free_route(self.model):
            raise CostPolicyError(
                f"openrouter model {self.model!r} is not an explicit ':free' route; refused by the free-only policy",
                code="LLM_PAID_MODEL_BLOCKED",
            )
        self._clock = clock
        self._verified_until = 0.0
        self._disabled_reason: str | None = None

    async def _before_request(self) -> None:
        if self._disabled_reason:
            raise CostPolicyError(self._disabled_reason, code="LLM_PAID_MODEL_BLOCKED")
        if self._clock() < self._verified_until:
            return
        try:
            async with self._client() as client:
                response = await client.get(f"{self._base_url}/models", headers=self._headers)
            catalog = response.json().get("data") if response.status_code < 400 else None
        except (httpx.HTTPError, ValueError) as exc:
            raise ExternalServiceError(f"openrouter catalog unreachable ({type(exc).__name__}); refusing to "
                                       "call a model whose price cannot be verified", code="LLM_FREE_CHECK_UNAVAILABLE") from exc
        if not isinstance(catalog, list):
            raise ExternalServiceError("openrouter catalog unavailable; refusing to call an unverified model",
                                       code="LLM_FREE_CHECK_UNAVAILABLE")
        entry = next((m for m in catalog if isinstance(m, dict) and m.get("id") == self.model), None)
        if entry is None:
            raise AgentError(f"openrouter model {self.model!r} is not in the catalog", code="LLM_MODEL_UNAVAILABLE",
                             category=ErrorCategory.AUTHENTICATION)
        if not is_zero_priced(entry.get("pricing")):
            self._disabled_reason = f"openrouter model {self.model!r} is not free; blocked by the free-only policy"
            logger.error("cost_policy: %s", self._disabled_reason)
            raise CostPolicyError(self._disabled_reason, code="LLM_PAID_MODEL_BLOCKED")
        self._verified_until = self._clock() + _FREE_CATALOG_TTL_SECONDS

    def _extra_body(self) -> dict[str, Any]:
        return {"provider": {"max_price": dict(_ZERO_PRICE)}, "usage": {"include": True}}

    def _after_response(self, payload: dict[str, Any]) -> None:
        cost = (payload.get("usage") or {}).get("cost")
        try:
            charged = float(cost) > 0 if cost is not None else False
        except (TypeError, ValueError):
            charged = False
        if charged:
            self._disabled_reason = f"openrouter reported a non-zero cost ({cost}); provider disabled"
            logger.error("cost_policy: %s", self._disabled_reason)


def is_free_route(model_id: str) -> bool:
    """OpenRouter's explicit free variants end in ':free'."""
    return model_id.endswith(":free")


def is_zero_priced(pricing: Any) -> bool:
    """True only if every price field present is exactly zero (missing pricing = not free)."""
    if not isinstance(pricing, dict) or not pricing:
        return False
    for value in pricing.values():
        try:
            if float(value) != 0.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def is_local_base_url(url: str) -> bool:
    """Ollama/LM Studio must run on this machine or the private network --
    a public endpoint could be a paid API."""
    host = (urlsplit(url).hostname or "").lower()
    if host in {"localhost", "host.docker.internal", "ollama"} or host.endswith((".local", ".localhost", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


class OllamaProvider(OpenAICompatibleProvider):
    """Local Ollama (or any local OpenAI-compatible server). Free because it
    runs on your own hardware; refuses non-local base URLs."""

    name = "ollama"

    def __init__(self, *, model: str | None, base_url: str = "http://localhost:11434/v1",
                 api_key: str | None = None, **kwargs: Any):
        if not is_local_base_url(base_url):
            raise CostPolicyError("OLLAMA_BASE_URL must point to a local/private host (a public URL could be a paid API)",
                                  code="LLM_NON_LOCAL_OLLAMA")
        super().__init__(api_key=api_key, model=model, base_url=base_url, **kwargs)


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------

def _check_dimensions(vectors: list[list[float]], expected: int, provider: str) -> list[list[float]]:
    for vector in vectors:
        if len(vector) != expected:
            raise ConfigurationError(
                f"{provider} returned a {len(vector)}-dimensional embedding, EMBEDDING_DIMENSION is {expected}",
                code="EMBEDDING_DIMENSION_MISMATCH",
            )
    return vectors


class OpenAIEmbeddingProvider:
    name = "openai"

    def __init__(self, *, api_key: str | None, model: str | None, dimension: int, client: Any = None):
        self.model = _require(model, "AGENT_EMBEDDING_MODEL (openai)")
        self._api_key = _require(api_key, "OPENAI_API_KEY")
        self.dimension = dimension
        self._client = client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._api_key)
        try:
            response = await self._client.embeddings.create(model=self.model, input=texts)
        except Exception as exc:  # noqa: BLE001 - mapped below
            status = getattr(exc, "status_code", None)
            if status:
                raise AgentError(f"openai embeddings HTTP {status}", code=f"OPENAI_HTTP_{status}",
                                 category=category_for_http_status(int(status))) from exc
            raise TransientError(f"openai embeddings failed: {type(exc).__name__}", code="EMBEDDING_FAILED") from exc
        return _check_dimensions([list(item.embedding) for item in response.data], self.dimension, self.name)


class GeminiEmbeddingProvider:
    name = "gemini"

    def __init__(self, *, api_key: str | None, model: str | None, dimension: int, client: Any = None):
        self.model = _require(model, "AGENT_EMBEDDING_MODEL (gemini)")
        self._api_key = _require(api_key, "GEMINI_API_KEY")
        self.dimension = dimension
        self._client = client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        from google.genai import errors as genai_errors
        from google.genai import types as genai_types

        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        try:
            response = await self._client.aio.models.embed_content(
                model=self.model,
                contents=texts,
                config=genai_types.EmbedContentConfig(output_dimensionality=self.dimension),
            )
        except genai_errors.APIError as exc:
            status = getattr(exc, "code", None) or 500
            raise AgentError(f"gemini embeddings HTTP {status}", code=f"GEMINI_HTTP_{status}",
                             category=category_for_http_status(int(status))) from exc
        vectors = [list(e.values or []) for e in (response.embeddings or [])]
        if len(vectors) != len(texts):
            raise DataError("gemini returned a different number of embeddings than inputs", code="EMBEDDING_COUNT_MISMATCH")
        return _check_dimensions(vectors, self.dimension, self.name)
