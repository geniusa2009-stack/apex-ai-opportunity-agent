"""Free-only multi-provider routing: fallback order, bounded retries,
retry-after, cooldowns, which failures move to the next provider, and the
three cost guards (Gemini allowlist, OpenRouter zero-price checks, Ollama
local-only). Everything is mocked: no keys, no network."""

from __future__ import annotations

import json

import httpx
import pytest

from app.agent.errors import (
    AgentError,
    AuthenticationError,
    CostPolicyError,
    DataError,
    ErrorCategory,
    ExternalServiceError,
    RateLimitError,
    TransientError,
    ValidationFailedError,
)
from app.agent.extraction import extract_opportunity
from app.agent.llm import LLMClient, build_llm_client, build_provider
from app.agent.llm.base import ChatMessage, LLMRequest
from app.agent.llm.fake import FakeLLMProvider
from app.agent.llm.providers import (
    OllamaProvider,
    OpenRouterProvider,
    http_error,
    is_local_base_url,
    is_zero_priced,
)
from app.agent.llm.router import CooldownRegistry, LLMRouter, RouterPolicy
from app.agent.normalization import extract_links
from tests.agent_support import SAMPLE_OPPORTUNITY_TEXT, sample_extraction
from tests.test_agent_config_and_security import _settings
from tests.test_agent_llm_and_extraction import FREE_PRICING, with_free_catalog

REQ = LLMRequest(system="sys", messages=[ChatMessage("user", "hi")])
OK = {"ok": True}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_router(*providers, policy=None, clock=None):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if clock is not None:
            clock.now += seconds

    router = LLMRouter(list(providers), policy or RouterPolicy(retries_per_provider=1, max_retry_wait_seconds=20),
                       cooldowns=CooldownRegistry(clock or Clock()), sleep=fake_sleep)
    return router, sleeps


def fake(name, *replies, model=None):
    return FakeLLMProvider(list(replies), name=name, model=model or f"{name}-model")


# --------------------------------------------------------------------------
# Order and failure handling
# --------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRouting:
    async def test_first_healthy_provider_answers_and_order_is_kept(self):
        a, b = fake("gemini", OK), fake("openrouter", OK)
        router, _ = make_router(a, b)
        response = await router.complete(REQ)
        assert response.provider == "gemini" and a.calls == 1 and b.calls == 0

    async def test_full_chain_order_gemini_gemini_openrouter_ollama(self):
        down = ExternalServiceError("503", code="GEMINI_HTTP_503")
        g1, g2 = fake("gemini", down, model="gemini-3.8-flash"), fake("gemini", down, model="gemini-3.5-flash-lite")
        o = fake("openrouter", AuthenticationError("402", code="OPENROUTER_HTTP_402"))
        local = fake("ollama", OK)
        router, _ = make_router(g1, g2, o, local, policy=RouterPolicy(retries_per_provider=0))
        response = await router.complete(REQ)
        assert response.provider == "ollama"
        assert (g1.calls, g2.calls, o.calls, local.calls) == (1, 1, 1, 1)

    async def test_429_is_retried_with_bounded_backoff_then_falls_back(self):
        limited = fake("gemini", RateLimitError("429", code="GEMINI_HTTP_429"))
        backup = fake("gemini2", OK)
        router, sleeps = make_router(limited, backup, policy=RouterPolicy(retries_per_provider=2, backoff_base_seconds=1))
        response = await router.complete(REQ)
        assert response.provider == "gemini2"
        assert limited.calls == 3 and sleeps == [1, 2]  # 1 + 2 retries, capped, never endless

    async def test_503_recovers_on_retry_without_fallback(self):
        flaky = fake("gemini", ExternalServiceError("503", code="GEMINI_HTTP_503"), OK)
        backup = fake("ollama", OK)
        router, sleeps = make_router(flaky, backup)
        response = await router.complete(REQ)
        assert response.provider == "gemini" and flaky.calls == 2 and backup.calls == 0 and len(sleeps) == 1

    async def test_server_retry_after_is_honoured_when_short(self):
        limited = fake("gemini", RateLimitError("429", code="X", retry_after=7), OK)
        router, sleeps = make_router(limited)
        await router.complete(REQ)
        assert sleeps == [7]

    async def test_long_retry_after_cools_down_instead_of_sleeping(self):
        clock = Clock()
        limited = fake("gemini", RateLimitError("429", code="X", retry_after=45))
        backup = fake("ollama", OK)
        router, sleeps = make_router(limited, backup, clock=clock)
        assert (await router.complete(REQ)).provider == "ollama"
        assert sleeps == [] and limited.calls == 1
        # within the cooldown the rate-limited slot is skipped entirely
        await router.complete(REQ)
        assert limited.calls == 1 and backup.calls == 2
        clock.now += 61  # cooldown = max(cooldown_seconds=60, retry_after=45)
        await router.complete(REQ)
        assert limited.calls == 2

    async def test_model_unavailable_404_moves_on_and_disables_slot(self):
        gone = fake("gemini", http_error("gemini", 404))
        assert gone._replies[0].code == "LLM_MODEL_UNAVAILABLE"
        backup = fake("gemini", OK, model="gemini-3.5-flash-lite")
        router, sleeps = make_router(gone, backup)
        await router.complete(REQ)
        await router.complete(REQ)
        assert gone.calls == 1 and sleeps == []  # no retry, then skipped

    async def test_truncated_output_tries_next_model(self):
        thinking = fake("gemini", DataError("cut", code="LLM_OUTPUT_TRUNCATED"))
        lite = fake("gemini", OK, model="gemini-3.5-flash-lite")
        router, _ = make_router(thinking, lite)
        assert (await router.complete(REQ)).model == "gemini-3.5-flash-lite"

    async def test_network_exceptions_are_classified_and_retried(self):
        class Boom:
            name, model, calls = "ollama", "m", 0

            async def complete(self, request):
                self.calls += 1
                raise TimeoutError("slow")

        boom = Boom()
        router, _ = make_router(boom, fake("x", OK))
        assert (await router.complete(REQ)).provider == "x" and boom.calls == 2


@pytest.mark.asyncio
class TestAllFail:
    async def test_transient_outage_everywhere_is_retryable_for_the_job_runner(self):
        router, _ = make_router(fake("a", RateLimitError("429", code="A", retry_after=5)),
                                fake("b", ExternalServiceError("503", code="B")))
        with pytest.raises(AgentError) as info:
            await router.complete(REQ)
        assert info.value.code == "LLM_ALL_PROVIDERS_FAILED" and info.value.retryable
        assert info.value.category == ErrorCategory.RATE_LIMIT

    async def test_only_config_or_cost_errors_mean_failed_not_retried(self):
        router, _ = make_router(fake("a", CostPolicyError("paid", code="LLM_PAID_MODEL_BLOCKED")),
                                fake("b", AuthenticationError("401", code="B")))
        with pytest.raises(AgentError) as info:
            await router.complete(REQ)
        assert info.value.category == ErrorCategory.AUTHENTICATION and not info.value.retryable

    async def test_only_truncation_goes_to_human_review(self):
        router, _ = make_router(fake("a", DataError("cut", code="LLM_OUTPUT_TRUNCATED")))
        with pytest.raises(AgentError) as info:
            await router.complete(REQ)
        assert info.value.code == "LLM_OUTPUT_TRUNCATED" and info.value.category == ErrorCategory.DATA


@pytest.mark.asyncio
class TestWithStructuredOutput:
    async def test_schema_validation_failure_never_triggers_provider_fallback(self):
        bad = fake("gemini", "not json at all")
        backup = fake("ollama", sample_extraction())
        router, _ = make_router(bad, backup)
        client = LLMClient(primary=router, max_validation_retries=1)
        with pytest.raises(ValidationFailedError):
            await extract_opportunity(client, text=SAMPLE_OPPORTUNITY_TEXT, links=extract_links(SAMPLE_OPPORTUNITY_TEXT))
        assert backup.calls == 0 and bad.calls == 2  # bounded self-correction only

    async def test_rate_limited_primary_falls_back_for_extraction(self):
        router, _ = make_router(fake("gemini", RateLimitError("429", code="X")), fake("openrouter", sample_extraction()))
        result = await extract_opportunity(LLMClient(primary=router), text=SAMPLE_OPPORTUNITY_TEXT,
                                           links=extract_links(SAMPLE_OPPORTUNITY_TEXT))
        assert result.provider == "openrouter"


# --------------------------------------------------------------------------
# Cost safety
# --------------------------------------------------------------------------

def test_gemini_model_outside_free_allowlist_is_refused_at_build():
    with pytest.raises(CostPolicyError):
        build_provider("gemini", "gemini-3.8-pro", _settings(gemini_api_key="g"))
    assert build_provider("gemini", "gemini-3.8-flash", _settings(gemini_api_key="g")).model == "gemini-3.8-flash"


def test_build_llm_client_builds_the_configured_chain_in_order():
    client = build_llm_client(_settings(
        agent_llm_chain="gemini:gemini-3.8-flash,gemini:gemini-3.5-flash-lite,openrouter:vendor/m:free,ollama:llama3.2",
        gemini_api_key="g", openrouter_api_key="o",
    ))
    assert client.primary.chain == ["gemini:gemini-3.8-flash", "gemini:gemini-3.5-flash-lite",
                                    "openrouter:vendor/m:free", "ollama:llama3.2"]
    assert build_llm_client(_settings()).available is False


def test_pricing_and_local_url_helpers():
    assert is_zero_priced(FREE_PRICING)
    assert not is_zero_priced({"prompt": "0", "completion": "0.000002"})
    assert not is_zero_priced({}) and not is_zero_priced(None) and not is_zero_priced({"prompt": "n/a"})
    for url in ("http://localhost:11434/v1", "http://127.0.0.1:11434/v1", "http://host.docker.internal:11434/v1",
                "http://ollama:11434/v1", "http://192.168.1.20:11434/v1", "http://mybox.local:1234/v1"):
        assert is_local_base_url(url), url
    for url in ("https://api.openai.com/v1", "https://openrouter.ai/api/v1", "http://8.8.8.8/v1"):
        assert not is_local_base_url(url), url


def test_ollama_refuses_public_endpoints():
    with pytest.raises(CostPolicyError):
        OllamaProvider(model="llama3.2", base_url="https://api.example.com/v1")
    assert OllamaProvider(model="llama3.2").model == "llama3.2"


@pytest.mark.asyncio
async def test_ollama_talks_openai_compatible_without_a_key():
    seen = {}

    def handler(request):
        if request.method == "GET":  # reachability probe
            return httpx.Response(200, json={"data": [{"id": "llama3.2"}]})
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    provider = OllamaProvider(model="llama3.2", transport=httpx.MockTransport(handler))
    response = await provider.complete(REQ)
    assert response.provider == "ollama" and seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["auth"] is None and "provider" not in seen["body"]


def _router_provider(handler, *, model="vendor/model:free", pricing=None, clock=None):
    kwargs = {"clock": clock} if clock else {}
    return OpenRouterProvider(api_key="sk-test", model=model,
                              transport=httpx.MockTransport(with_free_catalog(handler, model=model, pricing=pricing)),
                              **kwargs)


def _ok(request):
    return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {"cost": 0}})


@pytest.mark.asyncio
class TestOpenRouterFreeOnly:
    async def test_free_model_works_and_catalog_is_cached(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return _ok(request)

        provider = _router_provider(handler)
        await provider.complete(REQ)
        await provider.complete(REQ)
        assert calls == ["/api/v1/chat/completions"] * 2

    async def test_paid_model_is_blocked_before_any_chat_call(self):
        chats = []
        provider = _router_provider(lambda r: chats.append(1) or _ok(r), model="vendor/now-paid:free",
                                    pricing={"prompt": "0.000001", "completion": "0.000002", "request": "0"})
        for _ in range(2):  # stays blocked
            with pytest.raises(CostPolicyError) as info:
                await provider.complete(REQ)
            assert info.value.code == "LLM_PAID_MODEL_BLOCKED"
        assert chats == []

    async def test_zero_priced_but_not_a_free_route_is_refused(self):
        # live catalog (Oct 2026) had e.g. preview/audio models priced 0 per token without ':free'
        with pytest.raises(CostPolicyError) as info:
            OpenRouterProvider(api_key="k", model="google/some-preview-model")
        assert info.value.code == "LLM_PAID_MODEL_BLOCKED"
        with pytest.raises(CostPolicyError):
            OpenRouterProvider(api_key="k", model="openrouter/free")  # meta-router: cannot pin what it picks
        with pytest.raises(CostPolicyError):
            build_llm_client(_settings(agent_llm_chain="openrouter:vendor/model", openrouter_api_key="k"))

    async def test_missing_pricing_is_treated_as_paid(self):
        no_pricing = OpenRouterProvider(
            api_key="k", model="m:free",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": "m:free"}]})))
        with pytest.raises(CostPolicyError):
            await no_pricing.complete(REQ)

    async def test_catalog_unreachable_fails_closed(self):
        def handler(request):
            if request.url.path.endswith("/models"):
                raise httpx.ConnectError("down")
            raise AssertionError("chat must not be called when the price cannot be verified")

        provider = OpenRouterProvider(api_key="k", model="m:free", transport=httpx.MockTransport(handler))
        with pytest.raises(ExternalServiceError) as info:
            await provider.complete(REQ)
        assert info.value.code == "LLM_FREE_CHECK_UNAVAILABLE"

    async def test_model_not_in_catalog_is_unavailable(self):
        provider = OpenRouterProvider(
            api_key="k", model="gone/model:free",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []})))
        with pytest.raises(AgentError) as info:
            await provider.complete(REQ)
        assert info.value.code == "LLM_MODEL_UNAVAILABLE" and info.value.category == ErrorCategory.AUTHENTICATION

    async def test_reported_cost_disables_the_provider(self):
        def charged(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {"cost": 0.0001}})

        provider = _router_provider(charged)
        await provider.complete(REQ)  # already served; cannot be undone, but...
        with pytest.raises(CostPolicyError):
            await provider.complete(REQ)  # ...never again

    async def test_price_change_is_picked_up_after_cache_ttl(self):
        clock = Clock()
        state = {"pricing": FREE_PRICING}

        def handler(request):
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "m:free", "pricing": state["pricing"]}]})
            return _ok(request)

        provider = OpenRouterProvider(api_key="k", model="m:free", clock=clock, transport=httpx.MockTransport(handler))
        await provider.complete(REQ)
        state["pricing"] = {"prompt": "0.000003", "completion": "0"}
        clock.now += 3601
        with pytest.raises(CostPolicyError):
            await provider.complete(REQ)

    async def test_router_skips_blocked_openrouter_and_uses_local(self):
        paid = _router_provider(_ok, model="vendor/paid:free", pricing={"prompt": "0.00001", "completion": "0"})
        local = fake("ollama", OK)
        router, _ = make_router(paid, local)
        assert (await router.complete(REQ)).provider == "ollama"


def test_errors_never_contain_the_key():
    error = http_error("openrouter", 401)
    assert "sk-" not in str(error) and error.category == ErrorCategory.AUTHENTICATION
    assert http_error("gemini", 503, retry_after=3).retry_after == 3
    assert TransientError("x").retryable


# --------------------------------------------------------------------------
# HTTP 400 classification: no blanket fallback
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status,body,code,category", [
    (400, "API key not valid. Please pass a valid API key. [reason: API_KEY_INVALID]", "LLM_AUTH_INVALID",
     ErrorCategory.AUTHENTICATION),
    (400, "User location is not supported for the API use.", "LLM_PROVIDER_UNAVAILABLE", ErrorCategory.AUTHENTICATION),
    (400, "models/x is not found for API version v1beta, or is not supported for generateContent",
     "LLM_MODEL_UNAVAILABLE", ErrorCategory.AUTHENTICATION),
    (400, "vendor/old:free is not a valid model ID", "LLM_MODEL_UNAVAILABLE", ErrorCategory.AUTHENTICATION),
    (400, "This endpoint's maximum context length is 8192 tokens", "LLM_CONTEXT_TOO_LONG", ErrorCategory.DATA),
    (400, "Invalid value for 'temperature'", "LLM_INVALID_REQUEST", ErrorCategory.DATA),
    (400, "", "LLM_INVALID_REQUEST", ErrorCategory.DATA),
    (422, "messages: field required", "LLM_INVALID_REQUEST", ErrorCategory.DATA),
    (404, "", "LLM_MODEL_UNAVAILABLE", ErrorCategory.AUTHENTICATION),
    (429, "quota", "GEMINI_HTTP_429", ErrorCategory.RATE_LIMIT),
    (503, "high demand", "GEMINI_HTTP_503", ErrorCategory.EXTERNAL_SERVICE),
])
def test_http_errors_are_classified_explicitly(status, body, code, category):
    error = http_error("gemini", status, body=body)
    assert (error.code, error.category) == (code, category)


def test_error_snippet_is_redacted():
    error = http_error("openrouter", 400, body="bad request, Authorization: Bearer FAKE-TOKEN-NOT-A-KEY-0123456789")
    assert "FAKE-TOKEN-NOT-A-KEY" not in str(error)


@pytest.mark.asyncio
class TestRouter400Policy:
    async def test_generic_invalid_request_fails_clearly_without_fallback(self):
        bad = fake("gemini", http_error("gemini", 400, body="Invalid value for 'temperature'"))
        backup = fake("openrouter", OK)
        router, sleeps = make_router(bad, backup)
        with pytest.raises(DataError) as info:
            await router.complete(REQ)
        assert info.value.code == "LLM_INVALID_REQUEST"
        assert backup.calls == 0 and bad.calls == 1 and sleeps == []
        # the provider is NOT put on cooldown: the request was the problem
        assert router.cooldowns.get("gemini:gemini-model") is None

    async def test_context_too_long_tries_next_model(self):
        small = fake("ollama", http_error("ollama", 400, body="maximum context length is 4096 tokens"))
        big = fake("gemini", OK)
        router, _ = make_router(small, big)
        assert (await router.complete(REQ)).provider == "gemini"

    async def test_invalid_key_400_moves_on_and_disables(self):
        rejected = fake("gemini", http_error("gemini", 400, body="API key not valid [reason: API_KEY_INVALID]"))
        backup = fake("openrouter", OK)
        router, _ = make_router(rejected, backup)
        assert (await router.complete(REQ)).provider == "openrouter"
        await router.complete(REQ)
        assert rejected.calls == 1  # disabled, not retried

    async def test_openrouter_400_body_is_classified(self):
        def handler(request):
            return httpx.Response(400, json={"error": {"code": 400, "message": "Provider returned error",
                                                       "metadata": {"raw": "context length exceeded"}}})

        with pytest.raises(DataError) as info:
            await _router_provider(handler).complete(REQ)
        assert info.value.code == "LLM_CONTEXT_TOO_LONG"

        def bad(request):
            return httpx.Response(400, json={"error": {"code": 400, "message": "messages must be an array"}})

        with pytest.raises(DataError) as info:
            await _router_provider(bad).complete(REQ)
        assert info.value.code == "LLM_INVALID_REQUEST"

    async def test_gemini_sdk_400_invalid_key_is_auth(self):
        from google.genai import errors as genai_errors

        from app.agent.llm.providers import GeminiProvider
        from tests.test_agent_llm_and_extraction import _FakeGeminiClient

        def raise_400():
            raise genai_errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                           "message": "API key not valid. Please pass a valid API key."}})

        with pytest.raises(AgentError) as info:
            await GeminiProvider(api_key="g", model="m", client=_FakeGeminiClient(raise_400)).complete(REQ)
        assert info.value.code == "LLM_AUTH_INVALID" and info.value.category == ErrorCategory.AUTHENTICATION
