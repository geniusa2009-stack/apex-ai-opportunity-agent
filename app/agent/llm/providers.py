"""LLM / embedding provider adapters.

* `GeminiProvider` -- google-genai SDK (already a backend dependency),
  same call shape the existing aggregator uses.
* `OpenRouterProvider` -- any OpenAI-compatible ``/chat/completions``
  endpoint over plain HTTPS (OpenRouter, or a self-hosted server such as
  Ollama / LM Studio / vLLM by changing OPENROUTER_BASE_URL).
* `OpenAIEmbeddingProvider`, `GeminiEmbeddingProvider` -- embeddings,
  with a runtime check that every returned vector has the configured size.

Model names always come from configuration; nothing here assumes one.
Every error is mapped to an `AgentError` category so the job runner can
decide between retry / dead letter / human review.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.agent.errors import (
    AgentError,
    ConfigurationError,
    DataError,
    ExternalServiceError,
    TransientError,
    category_for_http_status,
)
from app.agent.llm.base import LLMRequest, LLMResponse


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
            status = getattr(exc, "code", None) or 500
            raise AgentError(
                f"gemini API error {status}", code=f"GEMINI_HTTP_{status}", category=category_for_http_status(int(status))
            ) from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TransientError(f"gemini network error: {type(exc).__name__}", code="LLM_NETWORK_ERROR") from exc
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
# OpenRouter / OpenAI-compatible
# --------------------------------------------------------------------------

class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str | None,
        base_url: str = "https://openrouter.ai/api/v1",
        app_title: str | None = None,
        http_referer: str | None = None,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = _require(model, "AGENT_LLM_MODEL (openrouter)")
        self._api_key = _require(api_key, "OPENROUTER_API_KEY")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport
        self._headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        if app_title:
            self._headers["X-OpenRouter-Title"] = app_title
            self._headers["X-Title"] = app_title  # older header name, still accepted
        if http_referer:
            self._headers["HTTP-Referer"] = http_referer

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            return await client.post(f"{self._base_url}/chat/completions", json=body, headers=self._headers)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        messages = [{"role": "system", "content": request.system}] + [
            {"role": m.role, "content": m.content} for m in request.messages
        ]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
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
            raise TransientError(f"openrouter network error: {type(exc).__name__}", code="LLM_NETWORK_ERROR") from exc

        if response.status_code >= 400:
            raise AgentError(
                f"openrouter HTTP {response.status_code}",
                code=f"OPENROUTER_HTTP_{response.status_code}",
                category=category_for_http_status(response.status_code),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalServiceError("openrouter returned non-JSON body", code="LLM_BAD_RESPONSE") from exc
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"] or {}
            status = int(error.get("code") or 502) if str(error.get("code", "")).isdigit() else 502
            raise AgentError(
                f"openrouter error: {str(error.get('message', ''))[:200]}",
                code=f"OPENROUTER_ERROR_{status}",
                category=category_for_http_status(status),
            )
        try:
            choice = payload["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExternalServiceError("openrouter response has no choices", code="LLM_BAD_RESPONSE") from exc
        if not text:
            raise ExternalServiceError("openrouter returned empty content", code="LLM_EMPTY_RESPONSE")
        usage = payload.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=str(payload.get("model") or self.model),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )


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
