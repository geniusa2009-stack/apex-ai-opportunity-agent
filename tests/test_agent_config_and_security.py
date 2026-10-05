"""Phase 1 -- security + configuration hardening.

- `.dockerignore` really keeps secrets and local environments out of the
  Docker build context (parsed and checked against the actual sensitive
  file names found in this repository).
- The Dockerfile no longer runs as root and declares a HEALTHCHECK.
- `collect_config_problems` catches the misconfigurations that would
  otherwise fail late or silently (embedding dimension mismatch, provider
  without a model name, LLM writer without an LLM, WhatsApp connector
  without its secrets, unbounded retries) and never leaks a secret value.
- `/health` stays dependency-free; `/ready` reports 503 with named checks
  when the database/Redis are unreachable, without exposing URLs.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.config_checks import (
    collect_config_problems,
    model_vector_dimension,
    run_startup_checks,
)
from app.core.config import Settings

REPO = Path(__file__).resolve().parents[1]


def _settings(**overrides) -> Settings:
    base = dict(
        supabase_jwt_secret="x" * 32,
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/db",
        sync_database_url="postgresql://u:p@127.0.0.1:1/db",
        redis_url="redis://127.0.0.1:1/0",
        celery_broker_url="redis://127.0.0.1:1/0",
        celery_result_backend="redis://127.0.0.1:1/1",
        supabase_url="http://localhost",
        supabase_service_role_key="not-a-real-key",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _fatal_settings(problems) -> set[str]:
    return {p.setting for p in problems if p.severity == "fatal"}


# --------------------------------------------------------------------------
# Docker
# --------------------------------------------------------------------------

def _dockerignore_patterns() -> list[str]:
    lines = (REPO / ".dockerignore").read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _is_ignored(path: str, patterns: list[str]) -> bool:
    ignored = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        pat = pattern[1:] if negate else pattern
        pat = pat.rstrip("/")
        if fnmatch.fnmatch(path, pat) or path.startswith(pat + "/") or fnmatch.fnmatch(path.split("/")[0], pat):
            ignored = not negate
    return ignored


@pytest.mark.parametrize(
    "path",
    [".env", ".env.bak_realenv", ".env.verify_tmp", ".env.production", ".venv/bin/python", ".git/config",
     "whatsapp_auth/creds.json", "__pycache__/x.pyc"],
)
def test_dockerignore_excludes_secrets_and_local_envs(path):
    assert _is_ignored(path, _dockerignore_patterns()), f"{path} would be copied into the image"


@pytest.mark.parametrize("path", [".env.example", "app/main.py", "requirements.txt", "alembic/env.py"])
def test_dockerignore_keeps_what_the_image_needs(path):
    assert not _is_ignored(path, _dockerignore_patterns())


def test_dockerfile_runs_unprivileged_with_healthcheck():
    dockerfile = (REPO / "Dockerfile").read_text()
    user_lines = [line.split()[1] for line in dockerfile.splitlines() if line.startswith("USER ")]
    assert user_lines and user_lines[-1] != "root"
    assert "HEALTHCHECK" in dockerfile
    assert "--timeout-graceful-shutdown" in dockerfile


# --------------------------------------------------------------------------
# Config checks
# --------------------------------------------------------------------------

def test_default_settings_have_no_fatal_problems():
    problems = collect_config_problems(_settings())
    assert not _fatal_settings(problems)
    run_startup_checks(_settings())  # must not raise


def test_embedding_dimension_mismatch_with_schema_is_fatal():
    problems = collect_config_problems(_settings(embedding_dimension=768))
    assert "EMBEDDING_DIMENSION" in _fatal_settings(problems)
    with pytest.raises(RuntimeError, match="EMBEDDING_DIMENSION"):
        run_startup_checks(_settings(embedding_dimension=768))


def test_known_embedding_model_with_wrong_dimension_is_fatal():
    problems = collect_config_problems(
        _settings(agent_embedding_provider="gemini", agent_embedding_model="text-embedding-004", gemini_api_key="k")
    )
    assert "AGENT_EMBEDDING_MODEL" in _fatal_settings(problems)


def test_model_vector_dimension_reads_the_real_schema():
    assert model_vector_dimension() == 1536


def test_llm_provider_without_model_or_key_is_fatal_and_no_model_is_assumed():
    fatal = _fatal_settings(collect_config_problems(_settings(agent_llm_provider="openrouter")))
    assert {"AGENT_LLM_MODEL", "OPENROUTER_API_KEY"} <= fatal


def test_llm_provider_fully_configured_is_fine():
    problems = collect_config_problems(
        _settings(agent_llm_provider="gemini", agent_llm_model="gemini-3.5-flash-lite", gemini_api_key="secret-value-123")
    )
    assert not _fatal_settings(problems)


def test_gemini_model_outside_free_allowlist_is_fatal():
    problems = collect_config_problems(
        _settings(agent_llm_chain="gemini:gemini-3.8-pro", gemini_api_key="secret-value-123")
    )
    assert "AGENT_LLM_CHAIN" in _fatal_settings(problems)


def test_full_free_chain_is_fine_and_warns_about_gemini_billing():
    problems = collect_config_problems(
        _settings(
            agent_llm_chain="gemini:gemini-3.8-flash,gemini:gemini-3.5-flash-lite,"
            "openrouter:vendor/model:free,ollama:llama3.2",
            gemini_api_key="g-secret",
            openrouter_api_key="or-secret",
        )
    )
    assert not _fatal_settings(problems)
    assert any(p.setting == "GEMINI_API_KEY" and p.severity == "warning" for p in problems)


def test_chain_problems_unknown_provider_missing_key_public_ollama():
    fatal = _fatal_settings(
        collect_config_problems(
            _settings(agent_llm_chain="openai:gpt-x,openrouter:vendor/m:free,ollama:m",
                      ollama_base_url="https://api.example.com/v1")
        )
    )
    assert {"AGENT_LLM_CHAIN", "OPENROUTER_API_KEY", "OLLAMA_BASE_URL"} <= fatal


def test_chain_entries_parse_openrouter_free_suffix_and_legacy_pair():
    cfg = _settings(agent_llm_chain=" gemini:gemini-3.8-flash , openrouter:vendor/model:free ")
    assert cfg.agent_llm_chain_entries == [("gemini", "gemini-3.8-flash"), ("openrouter", "vendor/model:free")]
    legacy = _settings(agent_llm_provider="gemini", agent_llm_model="a",
                       agent_llm_fallback_provider="ollama", agent_llm_fallback_model="b")
    assert legacy.agent_llm_chain_entries == [("gemini", "a"), ("ollama", "b")]


def test_router_retries_are_bounded():
    assert "AGENT_LLM_RETRIES_PER_PROVIDER" in _fatal_settings(
        collect_config_problems(_settings(agent_llm_retries_per_provider=10))
    )


def test_unknown_provider_is_fatal():
    assert "AGENT_LLM_PROVIDER" in _fatal_settings(collect_config_problems(_settings(agent_llm_provider="magic")))


def test_llm_writer_requires_llm():
    assert "AGENT_WRITER_MODE" in _fatal_settings(collect_config_problems(_settings(agent_writer_mode="llm")))


def test_unbounded_retries_are_rejected():
    assert "AGENT_JOB_MAX_ATTEMPTS" in _fatal_settings(collect_config_problems(_settings(agent_job_max_attempts=0)))
    assert "AGENT_JOB_MAX_ATTEMPTS" in _fatal_settings(collect_config_problems(_settings(agent_job_max_attempts=1000)))
    assert "AGENT_LLM_MAX_VALIDATION_RETRIES" in _fatal_settings(
        collect_config_problems(_settings(agent_llm_max_validation_retries=50))
    )


def test_whatsapp_connectors_require_their_secrets():
    assert "WHATSAPP_BRIDGE_HMAC_SECRET" in _fatal_settings(
        collect_config_problems(_settings(whatsapp_connector="bridge"))
    )
    fatal = _fatal_settings(collect_config_problems(_settings(whatsapp_connector="cloud_api")))
    assert {"WHATSAPP_CLOUD_APP_SECRET", "WHATSAPP_CLOUD_VERIFY_TOKEN"} <= fatal


def test_mock_whatsapp_is_refused_in_production():
    assert "WHATSAPP_CONNECTOR" in _fatal_settings(
        collect_config_problems(_settings(whatsapp_connector="mock", environment="production"))
    )


def test_telegram_publisher_requires_token_and_chat():
    assert "TELEGRAM_BOT_TOKEN / TELEGRAM_PUBLISH_CHAT_ID" in _fatal_settings(
        collect_config_problems(_settings(agent_publishers="manual,telegram"))
    )


def test_problem_messages_never_contain_secret_values():
    secret = "sk-super-secret-value-do-not-log"
    problems = collect_config_problems(
        _settings(
            agent_llm_provider="openrouter",
            openrouter_api_key=secret,
            whatsapp_connector="bridge",
            whatsapp_bridge_hmac_secret=None,
            embedding_dimension=3,
        )
    )
    assert problems
    assert all(secret not in p.message for p in problems)


# --------------------------------------------------------------------------
# Health / readiness
# --------------------------------------------------------------------------

def test_health_is_dependency_free():
    from app.main import app

    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_unavailable_dependencies_without_leaking_urls():
    from app.main import app

    client = TestClient(app)  # test env points DB/Redis at unreachable hosts
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"].startswith("unavailable")
    assert body["checks"]["redis"].startswith("unavailable")
    assert "postgresql" not in response.text and "redis://" not in response.text
    assert "@" not in response.text
