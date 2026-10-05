"""Startup / readiness configuration checks.

`collect_config_problems()` is pure (settings in, problems out) and is what
the tests exercise. `run_startup_checks()` raises on FATAL problems so a
misconfigured deployment fails at boot with a clear message instead of
corrupting data later (e.g. writing 768-dim vectors into a 1536 column).
Warnings are logged, never fatal. No secret value is ever included in a
message -- only the setting NAME.
"""

from __future__ import annotations

import dataclasses
import logging

from app.core.config import Settings
from app.core.config import settings as global_settings

logger = logging.getLogger(__name__)

SUPPORTED_LLM_PROVIDERS = frozenset({"gemini", "openrouter", "ollama", "none"})
SUPPORTED_EMBEDDING_PROVIDERS = frozenset({"openai", "gemini", "none"})
SUPPORTED_WRITER_MODES = frozenset({"template", "llm"})
SUPPORTED_WHATSAPP_CONNECTORS = frozenset({"disabled", "mock", "cloud_api", "bridge"})
SUPPORTED_PUBLISHERS = frozenset({"mock", "manual", "telegram"})

# Default output sizes of embedding models we know about. Used only to
# catch an obvious mismatch early; an unknown model is allowed (the runtime
# length check on every embedding call is the real guard). Models whose
# output size is configurable are not listed.
KNOWN_EMBEDDING_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-ada-002": 1536,
    "text-embedding-004": 768,
}


@dataclasses.dataclass(frozen=True)
class ConfigProblem:
    severity: str  # "fatal" | "warning"
    setting: str
    message: str


def model_vector_dimension() -> int:
    """The pgvector dimension the ORM model (and therefore the migrations)
    declare for `opportunities.embedding`."""
    from app.models.opportunity import Opportunity

    return int(Opportunity.__table__.c.embedding.type.dim)  # type: ignore[attr-defined]


def collect_config_problems(cfg: Settings) -> list[ConfigProblem]:
    problems: list[ConfigProblem] = []

    def fatal(setting: str, message: str) -> None:
        problems.append(ConfigProblem("fatal", setting, message))

    def warn(setting: str, message: str) -> None:
        problems.append(ConfigProblem("warning", setting, message))

    # --- Embeddings ---------------------------------------------------
    model_dim = model_vector_dimension()
    if cfg.embedding_dimension != model_dim:
        fatal(
            "EMBEDDING_DIMENSION",
            f"EMBEDDING_DIMENSION={cfg.embedding_dimension} but the database schema "
            f"(opportunities.embedding) is vector({model_dim}). Changing the dimension "
            "needs a migration that resizes the column and re-embeds existing rows.",
        )
    for setting_name, model_name in (
        ("AI_EMBEDDING_MODEL", cfg.ai_embedding_model),
        ("AGENT_EMBEDDING_MODEL", cfg.agent_embedding_model if cfg.agent_embedding_provider != "none" else None),
    ):
        known = KNOWN_EMBEDDING_DIMENSIONS.get(model_name or "")
        if known is not None and known != cfg.embedding_dimension:
            fatal(
                setting_name,
                f"{setting_name}={model_name!r} returns {known}-dimensional vectors but "
                f"EMBEDDING_DIMENSION={cfg.embedding_dimension}.",
            )

    if cfg.agent_embedding_provider not in SUPPORTED_EMBEDDING_PROVIDERS:
        fatal("AGENT_EMBEDDING_PROVIDER", f"must be one of {sorted(SUPPORTED_EMBEDDING_PROVIDERS)}")
    elif cfg.agent_embedding_provider != "none":
        if not cfg.agent_embedding_model:
            fatal("AGENT_EMBEDDING_MODEL", "required when AGENT_EMBEDDING_PROVIDER is set (no model name is assumed)")
        key_setting = {"openai": ("OPENAI_API_KEY", cfg.openai_api_key), "gemini": ("GEMINI_API_KEY", cfg.gemini_api_key)}
        name, value = key_setting[cfg.agent_embedding_provider]
        if not value:
            fatal(name, f"required by AGENT_EMBEDDING_PROVIDER={cfg.agent_embedding_provider}")

    # --- LLM (free-only routing chain) --------------------------------
    from app.agent.llm.providers import is_local_base_url

    chain_setting = "AGENT_LLM_CHAIN" if cfg.agent_llm_chain.strip() else "AGENT_LLM_PROVIDER"
    for provider_setting in ("AGENT_LLM_PROVIDER", "AGENT_LLM_FALLBACK_PROVIDER"):
        value = (cfg.agent_llm_provider if provider_setting == "AGENT_LLM_PROVIDER"
                 else cfg.agent_llm_fallback_provider) or "none"
        if value.lower() not in SUPPORTED_LLM_PROVIDERS:
            fatal(provider_setting, f"must be one of {sorted(SUPPORTED_LLM_PROVIDERS)}")
    llm_entries = cfg.agent_llm_chain_entries
    for provider, model in llm_entries:
        if provider not in SUPPORTED_LLM_PROVIDERS - {"none"}:
            fatal(chain_setting, f"unknown provider {provider!r}; supported: {sorted(SUPPORTED_LLM_PROVIDERS - {'none'})}")
            continue
        if not model:
            fatal("AGENT_LLM_CHAIN" if cfg.agent_llm_chain.strip() else "AGENT_LLM_MODEL", f"{provider} entry has no model (no model name is assumed)")
        if provider == "gemini":
            if not cfg.gemini_api_key:
                fatal("GEMINI_API_KEY", f"required by {chain_setting} (gemini)")
            if model and model not in cfg.agent_gemini_free_model_set:
                fatal(chain_setting, f"gemini model {model!r} is not on AGENT_GEMINI_FREE_MODELS (free-tier "
                                     "allowlist); refusing a possibly paid model")
        if provider == "openrouter" and not cfg.openrouter_api_key:
            fatal("OPENROUTER_API_KEY", f"required by {chain_setting} (openrouter)")
        if provider == "ollama" and not is_local_base_url(cfg.ollama_base_url):
            fatal("OLLAMA_BASE_URL", "must be a local/private address (a public URL could be a paid API)")
    if not llm_entries:
        warn(
            "AGENT_LLM_CHAIN",
            "no LLM configured: messages will stop at extraction with a clear "
            "configuration error (deterministic stages still run).",
        )
    if any(p == "gemini" for p, _ in llm_entries):
        warn(
            "GEMINI_API_KEY",
            "the Gemini free tier cannot be detected from the API: keep this key's project on the free tier "
            "(do not 'Upgrade to Paid' / attach billing), otherwise usage is billed.",
        )
    if not 0 <= cfg.agent_llm_retries_per_provider <= 3:
        fatal("AGENT_LLM_RETRIES_PER_PROVIDER", "must be between 0 and 3 (no endless retries)")
    if not 0 < cfg.agent_llm_max_retry_wait_seconds <= 120:
        fatal("AGENT_LLM_MAX_RETRY_WAIT_SECONDS", "must be > 0 and <= 120")
    if not 0 <= cfg.agent_llm_max_validation_retries <= 5:
        fatal("AGENT_LLM_MAX_VALIDATION_RETRIES", "must be between 0 and 5 (bounded self-correction)")

    # --- Writer / reviewer -------------------------------------------
    if cfg.agent_writer_mode not in SUPPORTED_WRITER_MODES:
        fatal("AGENT_WRITER_MODE", f"must be one of {sorted(SUPPORTED_WRITER_MODES)}")
    if cfg.agent_writer_mode == "llm" and not llm_entries:
        fatal("AGENT_WRITER_MODE", "llm writer needs AGENT_LLM_CHAIN")
    if cfg.agent_reviewer_llm_enabled and not llm_entries:
        fatal("AGENT_REVIEWER_LLM_ENABLED", "LLM reviewer needs AGENT_LLM_CHAIN")
    if not 0 <= cfg.agent_max_revision_rounds <= 3:
        fatal("AGENT_MAX_REVISION_ROUNDS", "must be between 0 and 3")

    # --- Jobs -----------------------------------------------------------
    if not 1 <= cfg.agent_job_max_attempts <= 20:
        fatal("AGENT_JOB_MAX_ATTEMPTS", "must be between 1 and 20 (no infinite retries)")
    if cfg.agent_job_backoff_base_seconds <= 0 or cfg.agent_job_backoff_max_seconds < cfg.agent_job_backoff_base_seconds:
        fatal("AGENT_JOB_BACKOFF_*", "base must be > 0 and max >= base")
    if cfg.agent_job_lease_seconds < 30:
        fatal("AGENT_JOB_LEASE_SECONDS", "must be >= 30")

    # --- Publishing ------------------------------------------------------
    unknown = [p for p in cfg.agent_publisher_names if p not in SUPPORTED_PUBLISHERS]
    if unknown:
        fatal("AGENT_PUBLISHERS", f"unknown publisher(s) {unknown}; supported: {sorted(SUPPORTED_PUBLISHERS)}")
    if "telegram" in cfg.agent_publisher_names and not (cfg.telegram_bot_token and cfg.telegram_publish_chat_id):
        fatal("TELEGRAM_BOT_TOKEN / TELEGRAM_PUBLISH_CHAT_ID", "required when the telegram publisher is enabled")
    if "mock" in cfg.agent_publisher_names and cfg.environment == "production":
        warn("AGENT_PUBLISHERS", "the mock publisher is enabled in production")

    # --- WhatsApp ---------------------------------------------------------
    if cfg.whatsapp_connector not in SUPPORTED_WHATSAPP_CONNECTORS:
        fatal("WHATSAPP_CONNECTOR", f"must be one of {sorted(SUPPORTED_WHATSAPP_CONNECTORS)}")
    elif cfg.whatsapp_connector in ("cloud_api", "bridge"):
        if cfg.whatsapp_connector == "bridge" and not cfg.whatsapp_bridge_hmac_secret:
            fatal("WHATSAPP_BRIDGE_HMAC_SECRET", "required for the bridge connector (webhook signature)")
        if cfg.whatsapp_connector == "cloud_api":
            if not cfg.whatsapp_cloud_app_secret:
                fatal("WHATSAPP_CLOUD_APP_SECRET", "required to verify X-Hub-Signature-256")
            if not cfg.whatsapp_cloud_verify_token:
                fatal("WHATSAPP_CLOUD_VERIFY_TOKEN", "required for the webhook verification handshake")
        if not cfg.allowed_whatsapp_group_id_set and not cfg.allowed_whatsapp_sender_id_set:
            warn("ALLOWED_WHATSAPP_GROUP_IDS", "allowlists are empty: every WhatsApp message will be refused")
        if not cfg.whatsapp_bot_id_set:
            warn("WHATSAPP_BOT_IDS", "bot id not set: loop protection relies on the from_me flag only")
    if cfg.whatsapp_connector == "mock" and cfg.environment == "production":
        fatal("WHATSAPP_CONNECTOR", "the mock WhatsApp connector must not run in production")

    # --- General ----------------------------------------------------------
    if cfg.environment == "production" and cfg.secret_key == "change_me_in_production":
        warn("SECRET_KEY", "still the placeholder value in production")

    return problems


def run_startup_checks(cfg: Settings | None = None) -> list[ConfigProblem]:
    """Log every problem; raise `RuntimeError` listing the fatal ones."""
    cfg = cfg or global_settings
    problems = collect_config_problems(cfg)
    for problem in problems:
        log = logger.error if problem.severity == "fatal" else logger.warning
        log("config %s: %s -- %s", problem.severity, problem.setting, problem.message)
    fatal = [p for p in problems if p.severity == "fatal"]
    if fatal:
        details = "; ".join(f"{p.setting}: {p.message}" for p in fatal)
        raise RuntimeError(f"Invalid configuration ({len(fatal)} fatal problem(s)): {details}")
    return problems


async def database_vector_dimension(db) -> int | None:
    """Actual `opportunities.embedding` dimension in the live Postgres DB,
    or None when not on Postgres / column missing."""
    from sqlalchemy import text

    if db.bind.dialect.name != "postgresql":
        return None
    result = await db.execute(
        text(
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
            "JOIN pg_class c ON a.attrelid = c.oid "
            "WHERE c.relname = 'opportunities' AND a.attname = 'embedding' AND NOT a.attisdropped"
        )
    )
    row = result.first()
    if not row or not row[0]:
        return None
    formatted = str(row[0])  # e.g. "vector(1536)"
    if "(" not in formatted:
        return None
    return int(formatted.split("(", 1)[1].rstrip(")"))
