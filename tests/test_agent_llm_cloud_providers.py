"""Groq and Cloudflare Workers AI providers, the extended free-only chain,
and Ollama reachability. Everything is mocked: no keys, no network."""

from __future__ import annotations

import json

import httpx
import pytest

from app.agent.errors import (
    AgentError,
    ConfigurationError,
    CostPolicyError,
    DataError,
    ErrorCategory,
)
from app.agent.llm import build_llm_client, build_provider
from app.agent.llm.base import ChatMessage, LLMRequest
from app.agent.llm.fake import FakeLLMProvider
from app.agent.llm.providers import (
    CloudflareWorkersAIProvider,
    GroqProvider,
    OllamaProvider,
    http_error,
    seconds_until_utc_midnight,
)
from app.agent.llm.router import CooldownRegistry, LLMRouter, RouterPolicy
from tests.test_agent_config_and_security import _settings

REQ = LLMRequest(system="sys", messages=[ChatMessage("user", "hi")])
CHOICE = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3}}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --------------------------------------------------------------------------
# Groq
# --------------------------------------------------------------------------

GROQ_CATALOG = {"data": [
    {"id": "openai/gpt-oss-120b", "active": True, "output_modalities": ["text"],
     "supported_features": ["tools", "json_mode", "structured_outputs", "reasoning"]},
    {"id": "plain/no-json", "active": True, "supported_features": []},
    {"id": "old/retired", "active": False},
    {"id": "canopylabs/tts", "active": True, "output_modalities": ["audio"]},
]}


def groq(handler, model="openai/gpt-oss-120b", **kwargs):
    def wrapped(request):
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(200, json=GROQ_CATALOG)
        return handler(request)

    return GroqProvider(api_key="gsk-test", model=model, free_tier_confirmed=True,
                        transport=httpx.MockTransport(wrapped), **kwargs)


@pytest.mark.asyncio
class TestGroq:
    async def test_success_request_shape_and_rate_limit_headers(self):
        seen = {}

        def handler(request):
            seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=CHOICE, headers={"x-ratelimit-limit-requests": "1000",
                                                             "x-ratelimit-remaining-requests": "999"})

        provider = groq(handler)
        response = await provider.complete(REQ)
        assert response.provider == "groq" and response.text == "{}"
        assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions" and seen["auth"] == "Bearer gsk-test"
        assert seen["body"]["response_format"] == {"type": "json_object"}
        assert "provider" not in seen["body"]  # no OpenRouter fields leak
        assert provider.last_rate_limits["x-ratelimit-limit-requests"] == "1000"

    async def test_json_mode_only_when_the_model_supports_it(self):
        bodies = []
        provider = groq(lambda r: bodies.append(json.loads(r.content)) or httpx.Response(200, json=CHOICE),
                        model="plain/no-json")
        await provider.complete(REQ)
        assert "response_format" not in bodies[0]

    @pytest.mark.parametrize("model", ["old/retired", "canopylabs/tts", "not/listed"])
    async def test_unavailable_models_are_refused_before_any_chat_call(self, model):
        chats = []
        provider = groq(lambda r: chats.append(1) or httpx.Response(200, json=CHOICE), model=model)
        with pytest.raises(AgentError) as info:
            await provider.complete(REQ)
        assert info.value.code == "LLM_MODEL_UNAVAILABLE" and chats == []

    async def test_catalog_unreachable_fails_closed(self):
        def handler(request):
            raise httpx.ConnectError("down")

        provider = GroqProvider(api_key="k", model="m", free_tier_confirmed=True,
                                transport=httpx.MockTransport(handler))
        with pytest.raises(AgentError) as info:
            await provider.complete(REQ)
        assert info.value.code == "LLM_FREE_CHECK_UNAVAILABLE"

    @pytest.mark.parametrize("status,body,code,category", [
        (429, {"error": {"message": "Rate limit reached for model", "type": "tokens", "code": "rate_limit_exceeded"}},
         "GROQ_HTTP_429", ErrorCategory.RATE_LIMIT),
        (503, {"error": {"message": "Service Unavailable"}}, "GROQ_HTTP_503", ErrorCategory.EXTERNAL_SERVICE),
        (400, {"error": {"message": "The model `x` has been decommissioned", "code": "model_decommissioned"}},
         "LLM_MODEL_UNAVAILABLE", ErrorCategory.AUTHENTICATION),
        (401, {"error": {"message": "Invalid API Key", "code": "invalid_api_key"}}, "GROQ_HTTP_401",
         ErrorCategory.AUTHENTICATION),
        (400, {"error": {"message": "Failed to generate JSON", "code": "json_validate_failed"}},
         "LLM_JSON_GENERATION_FAILED", ErrorCategory.DATA),
        (413, {"error": {"message": "Request too large for model on tokens per minute (TPM)"}},
         "LLM_CONTEXT_TOO_LONG", ErrorCategory.DATA),
        (400, {"error": {"message": "'messages' must contain the word 'json'", "type": "invalid_request_error"}},
         "LLM_INVALID_REQUEST", ErrorCategory.DATA),
    ])
    async def test_errors_are_classified(self, status, body, code, category):
        with pytest.raises(AgentError) as info:
            await groq(lambda r: httpx.Response(status, json=body)).complete(REQ)
        assert (info.value.code, info.value.category) == (code, category)
        assert "gsk-test" not in str(info.value)

    async def test_truncation_is_preserved(self):
        truncated = {"choices": [{"message": {"content": "{\"a\""}, "finish_reason": "length"}]}
        with pytest.raises(DataError) as info:
            await groq(lambda r: httpx.Response(200, json=truncated)).complete(REQ)
        assert info.value.code == "LLM_OUTPUT_TRUNCATED"

    async def test_raw_reasoning_is_stripped_from_content(self):
        reply = {"choices": [{"message": {"content": "<think>{maybe} hmm</think>\n{\"ok\": true}"},
                              "finish_reason": "stop"}]}
        response = await groq(lambda r: httpx.Response(200, json=reply)).complete(REQ)
        assert response.text == '{"ok": true}'


def test_groq_requires_key_and_free_tier_confirmation():
    with pytest.raises(ConfigurationError):
        GroqProvider(api_key=None, model="m", free_tier_confirmed=True)
    with pytest.raises(CostPolicyError) as info:
        GroqProvider(api_key="k", model="m")
    assert info.value.code == "LLM_FREE_TIER_UNCONFIRMED"
    with pytest.raises(ConfigurationError):
        GroqProvider(api_key="k", model=None, free_tier_confirmed=True)


# --------------------------------------------------------------------------
# Cloudflare Workers AI
# --------------------------------------------------------------------------

def _cf_model(name, **props):
    return {"name": name, "properties": [{"property_id": k, "value": v} for k, v in props.items()]}


CF_CATALOG = {"success": True, "result": [
    _cf_model("@cf/mistralai/mistral-small-3.1-24b-instruct", function_calling="true"),
    _cf_model("@cf/zai-org/glm-5.3", require_workers_paid="true"),
    _cf_model("@cf/old/model", planned_deprecation_date="2020-01-01"),
], "result_info": {"total_pages": 1}}


def cloudflare(handler, model="@cf/mistralai/mistral-small-3.1-24b-instruct", catalog=None):
    def wrapped(request):
        if request.url.path.endswith("/ai/models/search"):
            return httpx.Response(200, json=catalog or CF_CATALOG)
        return handler(request)

    return CloudflareWorkersAIProvider(api_token="cf-test", account_id="acc123", model=model,
                                       free_plan_confirmed=True, transport=httpx.MockTransport(wrapped))


@pytest.mark.asyncio
class TestCloudflare:
    async def test_success_and_endpoint(self):
        seen = {}

        def handler(request):
            seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=CHOICE)

        response = await cloudflare(handler).complete(REQ)
        assert response.provider == "cloudflare"
        assert seen["url"] == "https://api.cloudflare.com/client/v4/accounts/acc123/ai/v1/chat/completions"
        assert seen["auth"] == "Bearer cf-test" and seen["body"]["model"].startswith("@cf/")

    async def test_paid_only_model_is_blocked_and_stays_blocked(self):
        chats = []
        provider = cloudflare(lambda r: chats.append(1) or httpx.Response(200, json=CHOICE), model="@cf/zai-org/glm-5.3")
        for _ in range(2):
            with pytest.raises(CostPolicyError) as info:
                await provider.complete(REQ)
            assert info.value.code == "LLM_PAID_MODEL_BLOCKED"
        assert chats == []

    @pytest.mark.parametrize("model", ["@cf/old/model", "@cf/not/listed"])
    async def test_deprecated_or_unlisted_model_is_unavailable(self, model):
        with pytest.raises(AgentError) as info:
            await cloudflare(lambda r: httpx.Response(200, json=CHOICE), model=model).complete(REQ)
        assert info.value.code == "LLM_MODEL_UNAVAILABLE"

    async def test_catalog_failure_fails_closed(self):
        provider = cloudflare(lambda r: httpx.Response(200, json=CHOICE),
                              catalog={"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]})
        with pytest.raises(AgentError) as info:
            await provider.complete(REQ)
        assert info.value.code == "LLM_FREE_CHECK_UNAVAILABLE"

    async def test_daily_allocation_exhausted_is_rate_limit_until_midnight(self):
        body = {"success": False, "errors": [{"code": 4006, "message": "you have used up your daily free allocation "
                                                                     "of 10,000 neurons"}]}
        with pytest.raises(AgentError) as info:
            await cloudflare(lambda r: httpx.Response(429, json=body)).complete(REQ)
        assert info.value.code == "LLM_QUOTA_EXHAUSTED" and info.value.category == ErrorCategory.RATE_LIMIT
        assert 60 < info.value.retry_after <= 86400 + 60

    @pytest.mark.parametrize("status,code", [(503, "CLOUDFLARE_HTTP_503"), (429, "CLOUDFLARE_HTTP_429")])
    async def test_transient_errors(self, status, code):
        body = {"success": False, "errors": [{"code": 3040, "message": "Capacity temporarily exceeded"}]}
        with pytest.raises(AgentError) as info:
            await cloudflare(lambda r: httpx.Response(status, json=body)).complete(REQ)
        assert info.value.code == code and info.value.retryable

    async def test_invalid_request_is_not_hidden(self):
        body = {"success": False, "errors": [{"code": 5006, "message": "Error: required properties at '/' are 'messages'"}]}
        with pytest.raises(DataError) as info:
            await cloudflare(lambda r: httpx.Response(400, json=body)).complete(REQ)
        assert info.value.code == "LLM_INVALID_REQUEST"


def test_cloudflare_requires_credentials_and_plan_confirmation():
    with pytest.raises(ConfigurationError):
        CloudflareWorkersAIProvider(api_token=None, account_id="a", model="m", free_plan_confirmed=True)
    with pytest.raises(ConfigurationError):
        CloudflareWorkersAIProvider(api_token="t", account_id=None, model="m", free_plan_confirmed=True)
    with pytest.raises(CostPolicyError) as info:
        CloudflareWorkersAIProvider(api_token="t", account_id="a", model="m")
    assert info.value.code == "LLM_FREE_TIER_UNCONFIRMED"


def test_quota_classification_and_midnight_helper():
    assert seconds_until_utc_midnight(now=86400 * 10) == 86400 + 60
    assert seconds_until_utc_midnight(now=86400 * 10 + 86000) == 460
    assert http_error("cloudflare", 429, body="[4006] used up your daily free allocation").code == "LLM_QUOTA_EXHAUSTED"


# --------------------------------------------------------------------------
# Ollama reachability
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ollama_unreachable_is_skipped_quickly_with_cooldown():
    def down(request):
        raise httpx.ConnectError("connection refused")

    ollama = OllamaProvider(model="llama3.2", transport=httpx.MockTransport(down))
    with pytest.raises(AgentError) as info:
        await ollama.complete(REQ)
    assert info.value.code == "LLM_PROVIDER_UNREACHABLE" and info.value.retry_after == 300

    sleeps = []

    async def no_sleep(seconds):
        sleeps.append(seconds)

    clock = Clock()
    router = LLMRouter([ollama, FakeLLMProvider([{"ok": 1}], name="backup")],
                       RouterPolicy(retries_per_provider=1, max_retry_wait_seconds=20),
                       cooldowns=CooldownRegistry(clock), sleep=no_sleep)
    assert (await router.complete(REQ)).provider == "backup"
    assert sleeps == []  # no in-call waiting for a dead local server
    assert router.cooldowns.get("ollama:llama3.2") is not None


@pytest.mark.asyncio
async def test_ollama_all_unreachable_is_retryable_for_the_job():
    def down(request):
        raise httpx.ConnectError("refused")

    router = LLMRouter([OllamaProvider(model="m", transport=httpx.MockTransport(down))],
                       cooldowns=CooldownRegistry(Clock()))
    with pytest.raises(AgentError) as info:
        await router.complete(REQ)
    assert info.value.retryable and info.value.code == "LLM_ALL_PROVIDERS_FAILED"


# --------------------------------------------------------------------------
# Chain, order and startup checks
# --------------------------------------------------------------------------

FULL_CHAIN = ("gemini:gemini-3.8-flash,gemini:gemini-3.5-flash-lite,groq:openai/gpt-oss-120b,"
              "openrouter:vendor/a:free,openrouter:vendor/b:free,"
              "cloudflare:@cf/mistralai/mistral-small-3.1-24b-instruct,ollama:llama3.2")
FULL_SECRETS = {"gemini_api_key": "g", "groq_api_key": "q", "openrouter_api_key": "o", "cloudflare_api_token": "c",
                    "cloudflare_account_id": "a", "groq_free_tier_confirmed": True,
                    "cloudflare_workers_free_plan_confirmed": True}


def test_full_chain_is_built_in_configured_order():
    client = build_llm_client(_settings(agent_llm_chain=FULL_CHAIN, **FULL_SECRETS))
    assert [k.split(":")[0] for k in client.primary.chain] == [
        "gemini", "gemini", "groq", "openrouter", "openrouter", "cloudflare", "ollama"]


def test_unconfirmed_free_plans_block_build_and_startup():
    from app.agent.config_checks import collect_config_problems

    secrets = dict(FULL_SECRETS, groq_free_tier_confirmed=False, cloudflare_workers_free_plan_confirmed=False)
    with pytest.raises(CostPolicyError):
        build_llm_client(_settings(agent_llm_chain=FULL_CHAIN, **secrets))
    fatal = {p.setting for p in collect_config_problems(_settings(agent_llm_chain=FULL_CHAIN, **secrets))
             if p.severity == "fatal"}
    assert {"GROQ_FREE_TIER_CONFIRMED", "CLOUDFLARE_WORKERS_FREE_PLAN_CONFIRMED"} <= fatal
    ok = {p.setting for p in collect_config_problems(_settings(agent_llm_chain=FULL_CHAIN, **FULL_SECRETS))
          if p.severity == "fatal"}
    assert not ok


def test_missing_credentials_are_fatal_at_startup():
    from app.agent.config_checks import collect_config_problems

    fatal = {p.setting for p in collect_config_problems(
        _settings(agent_llm_chain="groq:m,cloudflare:@cf/m", groq_free_tier_confirmed=True,
                  cloudflare_workers_free_plan_confirmed=True)) if p.severity == "fatal"}
    assert {"GROQ_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"} <= fatal


def test_problem_messages_never_contain_new_secrets():
    from app.agent.config_checks import collect_config_problems

    problems = collect_config_problems(_settings(agent_llm_chain="groq:m,cloudflare:@cf/m",
                                                 groq_api_key="gsk-SECRET-1", cloudflare_api_token="cf-SECRET-2"))
    assert problems and all("SECRET" not in p.message for p in problems)


def test_build_provider_cloudflare_uses_account():
    p = build_provider("cloudflare", "@cf/x/y", _settings(**FULL_SECRETS))
    assert p.name == "cloudflare" and p.model == "@cf/x/y"
