"""Phase 4 -- LLM provider abstraction, structured output with bounded
validation self-correction, provider adapters (with mocked transports),
deterministic triage and grounded extraction."""

from __future__ import annotations

import json

import httpx
import pytest

from app.agent.errors import (
    AuthenticationError,
    ConfigurationError,
    ErrorCategory,
    RateLimitError,
    ValidationFailedError,
)
from app.agent.extraction import (
    ExtractedOpportunity,
    TriageDecision,
    extract_opportunity,
    grounding_context,
    triage,
)
from app.agent.llm import LLMClient, build_llm_client
from app.agent.llm.base import ChatMessage, LLMRequest
from app.agent.llm.fake import FakeLLMProvider
from app.agent.llm.providers import (
    GeminiProvider,
    OpenAIEmbeddingProvider,
    OpenRouterProvider,
)
from app.agent.llm.structured import parse_json_object
from app.agent.normalization import extract_links
from tests.agent_support import SAMPLE_OPPORTUNITY_TEXT, sample_extraction
from tests.test_agent_config_and_security import _settings

LINKS = extract_links(SAMPLE_OPPORTUNITY_TEXT)


def client_with(*replies, fallback=None, retries=2) -> tuple[LLMClient, FakeLLMProvider]:
    provider = FakeLLMProvider(list(replies))
    return LLMClient(primary=provider, fallback=fallback, max_validation_retries=retries), provider


# --------------------------------------------------------------------------
# Triage
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["شكرا", "تمام 👍", "صباح الخير يا جماعة", "ok", "👍👍", "حاضر"])
def test_triage_drops_chatter_without_llm(text):
    assert triage(text, extract_links(text)).decision == TriageDecision.CHATTER


def test_triage_flags_clear_and_uncertain_messages():
    assert triage(SAMPLE_OPPORTUNITY_TEXT, LINKS).decision == TriageDecision.OPPORTUNITY
    assert triage("منحة جديدة للطلاب، التفاصيل قريبا جدا", []).decision == TriageDecision.UNCERTAIN
    assert triage("شوفوا ده https://example.org/x", ["https://example.org/x"]).decision == TriageDecision.UNCERTAIN


# --------------------------------------------------------------------------
# JSON parsing
# --------------------------------------------------------------------------

def test_parse_json_object_accepts_fences_and_surrounding_prose():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": 1} hope it helps') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_object("no json here")


# --------------------------------------------------------------------------
# Structured generation + self-correction
# --------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStructured:
    async def test_valid_output_first_try(self):
        client, provider = client_with(sample_extraction())
        result = await extract_opportunity(client, text=SAMPLE_OPPORTUNITY_TEXT, links=LINKS)
        assert result.attempts == 1 and provider.calls == 1
        assert result.value.deadline == "2026-10-15"
        assert result.value.opportunity_type == "scholarship"

    async def test_malformed_json_is_corrected_with_errors_fed_back(self):
        client, provider = client_with("{not json", sample_extraction())
        result = await extract_opportunity(client, text=SAMPLE_OPPORTUNITY_TEXT, links=LINKS)
        assert result.attempts == 2
        second = provider.requests[1]
        assert [m.role for m in second.messages] == ["user", "assistant", "user"]
        assert "failed validation" in second.messages[-1].content

    async def test_schema_failure_retries_then_succeeds(self):
        bad = sample_extraction(opportunity_type="super-scholarship", age_min=40, age_max=20)
        client, provider = client_with(bad, sample_extraction())
        result = await extract_opportunity(client, text=SAMPLE_OPPORTUNITY_TEXT, links=LINKS)
        assert result.attempts == 2
        feedback = provider.requests[1].messages[-1].content
        assert "opportunity_type" in feedback

    async def test_max_retries_is_bounded_and_raises_validation_error(self):
        client, provider = client_with("still not json", retries=2)
        with pytest.raises(ValidationFailedError) as info:
            await extract_opportunity(client, text=SAMPLE_OPPORTUNITY_TEXT, links=LINKS)
        assert provider.calls == 3  # 1 + 2 retries, never more
        assert info.value.code == "LLM_SCHEMA_VALIDATION_FAILED"
        assert info.value.category == ErrorCategory.VALIDATION

    async def test_invented_link_is_rejected_and_corrected(self):
        invented = sample_extraction(application_url="https://daad-apply-now.example.com/form")
        client, provider = client_with(invented, sample_extraction())
        result = await extract_opportunity(client, text=SAMPLE_OPPORTUNITY_TEXT, links=LINKS)
        assert result.attempts == 2
        assert "links that appear in the message" in provider.requests[1].messages[-1].content
        assert "daad.de" in result.value.application_url

    async def test_invented_deadline_is_rejected(self):
        client, _ = client_with(sample_extraction(deadline="2026-12-31"), retries=0)
        with pytest.raises(ValidationFailedError, match="not a date written in the message"):
            await extract_opportunity(client, text=SAMPLE_OPPORTUNITY_TEXT, links=LINKS)

    async def test_missing_deadline_is_null_not_invented(self):
        text = "منحة دراسية من جامعة X للطلاب. قدم هنا https://x.example.edu/apply"
        client, _ = client_with(sample_extraction(deadline=None, application_url="https://x.example.edu/apply", official_url=None))
        result = await extract_opportunity(client, text=text, links=extract_links(text))
        assert result.value.deadline is None
        assert "deadline" in result.value.computed_missing_fields()

    async def test_not_opportunity_needs_no_title(self):
        client, _ = client_with({"is_opportunity": False, "not_opportunity_reason": "greeting"})
        result = await extract_opportunity(client, text="hello all", links=[])
        assert result.value.is_opportunity is False

    async def test_fallback_provider_used_on_rate_limit(self):
        fallback = FakeLLMProvider([sample_extraction()], name="backup")
        client, primary = client_with(RateLimitError("429", code="RATE_LIMITED"), fallback=fallback)
        result = await extract_opportunity(client, text=SAMPLE_OPPORTUNITY_TEXT, links=LINKS)
        assert result.provider == "backup" and primary.calls == 1 and fallback.calls == 1

    async def test_no_fallback_on_validation_failure(self):
        fallback = FakeLLMProvider([sample_extraction()], name="backup")
        client, _ = client_with("nope", fallback=fallback, retries=0)
        with pytest.raises(ValidationFailedError):
            await extract_opportunity(client, text=SAMPLE_OPPORTUNITY_TEXT, links=LINKS)
        assert fallback.calls == 0

    async def test_unconfigured_client_raises_configuration_error(self):
        with pytest.raises(ConfigurationError):
            await extract_opportunity(LLMClient(primary=None), text="x", links=[])


def test_grounding_context_and_schema_without_context():
    ctx = grounding_context(SAMPLE_OPPORTUNITY_TEXT, LINKS)
    assert any("daad.de" in u for u in ctx["allowed_urls"])
    # Without context (e.g. re-loading a stored extraction) the schema still validates types.
    assert ExtractedOpportunity.model_validate(sample_extraction(deadline="2030-01-01")).deadline == "2030-01-01"


def test_build_llm_client_from_settings():
    assert build_llm_client(_settings()).available is False
    client = build_llm_client(
        _settings(agent_llm_provider="openrouter", agent_llm_model="vendor/some-model:free", openrouter_api_key="k",
                  agent_llm_fallback_provider="gemini", agent_llm_fallback_model="gemini-3.5-flash-lite",
                  gemini_api_key="g")
    )
    # legacy primary/fallback pair becomes a two-slot router
    assert client.primary.name == "router"
    assert client.primary.chain == ["openrouter:vendor/some-model:free", "gemini:gemini-3.5-flash-lite"]
    with pytest.raises(ConfigurationError):
        build_llm_client(_settings(agent_llm_provider="openrouter", openrouter_api_key="k"))  # no model assumed


# --------------------------------------------------------------------------
# OpenRouter adapter (mocked HTTP)
# --------------------------------------------------------------------------

FREE_PRICING = {"prompt": "0", "completion": "0", "request": "0", "image": "0"}


def with_free_catalog(handler, model="vendor/model:free", pricing=None):
    """OpenRouterProvider reads the public model catalog first (free-only
    check); serve it, then delegate chat calls to `handler`."""

    def wrapped(request: httpx.Request):
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": model, "pricing": pricing or FREE_PRICING}]})
        return handler(request)

    return wrapped


def _openrouter(handler) -> OpenRouterProvider:
    return OpenRouterProvider(api_key="sk-test", model="vendor/model:free",
                              transport=httpx.MockTransport(with_free_catalog(handler)),
                              app_title="APEX", http_referer="https://apex.example")


REQ = LLMRequest(system="sys", messages=[ChatMessage("user", "hi")])


@pytest.mark.asyncio
class TestOpenRouter:
    async def test_success_and_request_shape(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"model": "vendor/model", "choices": [{"message": {"content": '{"ok": true}'}}],
                                             "usage": {"prompt_tokens": 3, "completion_tokens": 2}})

        response = await _openrouter(handler).complete(REQ)
        assert response.text == '{"ok": true}' and response.input_tokens == 3
        assert seen["url"].endswith("/chat/completions") and seen["auth"] == "Bearer sk-test"
        assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
        assert seen["body"]["response_format"] == {"type": "json_object"}
        # free-only guard is sent on every request
        assert seen["body"]["provider"]["max_price"] == {"prompt": 0, "completion": 0, "request": 0, "image": 0}

    @pytest.mark.parametrize("status,category", [(429, ErrorCategory.RATE_LIMIT), (401, ErrorCategory.AUTHENTICATION),
                                                 (402, ErrorCategory.AUTHENTICATION), (503, ErrorCategory.EXTERNAL_SERVICE)])
    async def test_http_errors_are_classified(self, status, category):
        provider = _openrouter(lambda r: httpx.Response(status, json={"error": {"message": "x"}}))
        with pytest.raises(Exception) as info:
            await provider.complete(REQ)
        assert info.value.category == category
        assert "sk-test" not in str(info.value)

    async def test_json_mode_unsupported_retries_without_it(self):
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append("response_format" in body)
            if "response_format" in body:
                return httpx.Response(400, text='{"error":{"message":"response_format not supported"}}')
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

        await _openrouter(handler).complete(REQ)
        assert calls == [True, False]

    async def test_network_error_is_transient(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        with pytest.raises(Exception) as info:
            await _openrouter(handler).complete(REQ)
        assert info.value.category == ErrorCategory.TRANSIENT


# --------------------------------------------------------------------------
# Gemini adapter (fake SDK client) and embeddings
# --------------------------------------------------------------------------

class _FakeGeminiModels:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.kwargs = None

    async def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return self.behaviour()


class _FakeGeminiClient:
    def __init__(self, behaviour):
        self.aio = type("Aio", (), {})()
        self.aio.models = _FakeGeminiModels(behaviour)


@pytest.mark.asyncio
async def test_gemini_adapter_maps_roles_and_errors():
    from google.genai import errors as genai_errors

    ok = _FakeGeminiClient(lambda: type("R", (), {"text": '{"a":1}', "usage_metadata": None})())
    provider = GeminiProvider(api_key="g", model="configured-model", client=ok)
    response = await provider.complete(LLMRequest(system="s", messages=[ChatMessage("user", "u"), ChatMessage("assistant", "a")]))
    assert response.text == '{"a":1}'
    assert [c["role"] for c in ok.aio.models.kwargs["contents"]] == ["user", "model"]
    assert ok.aio.models.kwargs["model"] == "configured-model"

    def raise_429():
        raise genai_errors.ClientError(429, {"error": {"message": "quota"}})

    with pytest.raises(RateLimitError.__mro__[1]) as info:
        await GeminiProvider(api_key="g", model="m", client=_FakeGeminiClient(raise_429)).complete(REQ)
    assert info.value.category == ErrorCategory.RATE_LIMIT


@pytest.mark.asyncio
async def test_embedding_dimension_is_checked_at_runtime():
    class FakeOpenAI:
        class embeddings:
            @staticmethod
            async def create(model, input):
                return type("Resp", (), {"data": [type("E", (), {"embedding": [0.1] * 768})() for _ in input]})()

    provider = OpenAIEmbeddingProvider(api_key="k", model="m", dimension=1536, client=FakeOpenAI())
    with pytest.raises(ConfigurationError) as info:
        await provider.embed(["hello"])
    assert info.value.code == "EMBEDDING_DIMENSION_MISMATCH"


def test_provider_requires_explicit_model_and_key():
    with pytest.raises(ConfigurationError):
        OpenRouterProvider(api_key="k", model=None)
    with pytest.raises(ConfigurationError):
        GeminiProvider(api_key=None, model="m")
    assert AuthenticationError("x").category == ErrorCategory.AUTHENTICATION


@pytest.mark.asyncio
async def test_truncated_output_is_never_returned_as_an_answer():
    """Live finding (gemini, thinking model): with a small max_output_tokens the
    hidden thinking tokens used the whole budget and the API returned a partial
    'APEX_' with finish_reason=MAX_TOKENS. Partial text must raise, not pass."""
    from google.genai import types as genai_types

    truncated = type("R", (), {
        "text": "APEX_",
        "usage_metadata": None,
        "candidates": [type("C", (), {"finish_reason": genai_types.FinishReason.MAX_TOKENS})()],
    })()
    with pytest.raises(Exception) as info:
        await GeminiProvider(api_key="g", model="m", client=_FakeGeminiClient(lambda: truncated)).complete(REQ)
    assert info.value.code == "LLM_OUTPUT_TRUNCATED" and info.value.category == ErrorCategory.DATA

    finished = type("R", (), {
        "text": "APEX_OK",
        "usage_metadata": None,
        "candidates": [type("C", (), {"finish_reason": genai_types.FinishReason.STOP})()],
    })()
    ok = await GeminiProvider(api_key="g", model="m", client=_FakeGeminiClient(lambda: finished)).complete(REQ)
    assert ok.text == "APEX_OK"

    provider = _openrouter(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "{\"a\""}, "finish_reason": "length"}]}))
    with pytest.raises(Exception) as info:
        await provider.complete(REQ)
    assert info.value.code == "LLM_OUTPUT_TRUNCATED"
