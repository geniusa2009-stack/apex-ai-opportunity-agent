# Environment variables — AI Opportunity Agent

All configuration comes from environment variables (`app/core/config.py`).
Secrets belong in `.env` (git- and docker-ignored) or the host's secret
store. Fatal misconfigurations stop the app at startup with the setting name
(`app/agent/config_checks.py`); secret values are never printed.

## Embeddings

| Variable | Default | Notes |
|---|---|---|
| `EMBEDDING_DIMENSION` | `1536` | Must equal `opportunities.embedding` = `vector(1536)`. Checked at startup, on every embedding call and by `/ready` against the live DB. Changing it needs a migration + re-embedding. |
| `AGENT_EMBEDDING_PROVIDER` | `none` | `openai`, `gemini`, `none`. `none` = dedup stays fully deterministic, RAG uses lexical similarity. |
| `AGENT_EMBEDDING_MODEL` | — | Required if a provider is set. Semantic dedup search over `opportunities.embedding` runs only when this equals `AI_EMBEDDING_MODEL` (vectors from different models are not comparable). |

## LLM

| Variable | Default | Notes |
|---|---|---|
| `AGENT_LLM_PROVIDER` | `none` | `gemini`, `openrouter`, `none` |
| `AGENT_LLM_MODEL` | — | **Required** with a provider. No model name is assumed. |
| `AGENT_LLM_FALLBACK_PROVIDER` / `_MODEL` | — | Tried once on transport/rate-limit/auth errors (not on validation failures). |
| `AGENT_LLM_TIMEOUT_SECONDS` | `60` | |
| `AGENT_LLM_TEMPERATURE` | `0.1` | |
| `AGENT_LLM_MAX_VALIDATION_RETRIES` | `2` | Self-correction rounds, 0–5. |
| `GEMINI_API_KEY` | — | existing setting, reused |
| `OPENROUTER_API_KEY` | — | |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Any OpenAI-compatible server, e.g. local Ollama `http://localhost:11434/v1` (zero cost). |
| `OPENROUTER_APP_TITLE`, `OPENROUTER_HTTP_REFERER` | | optional attribution headers |

Free tiers change; nothing in the agent depends on one staying free. With
no LLM at all, ingestion/dedup/verification still run; extraction reports
`LLM_NOT_CONFIGURED`.

## Writer / reviewer

| Variable | Default | Notes |
|---|---|---|
| `AGENT_WRITER_MODE` | `template` | `template` (0 AI calls) or `llm` (1 call + fact guard + template fallback) |
| `AGENT_REVIEWER_LLM_ENABLED` | `false` | extra LLM review (stricter only) |
| `AGENT_MAX_REVISION_ROUNDS` | `2` | 0–3 |

## Jobs

| Variable | Default | Notes |
|---|---|---|
| `AGENT_JOB_MAX_ATTEMPTS` | `5` | 1–20, then DEAD_LETTER |
| `AGENT_JOB_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | `30` / `3600` | capped exponential |
| `AGENT_JOB_LEASE_SECONDS` | `300` | crash recovery delay |
| `AGENT_JOB_BATCH_SIZE` | `10` | jobs per run |
| `AGENT_CELERY_TRIGGER_ENABLED` | `true` | kick the worker after ingestion/approval |

## Dedup / verification / feeds

| Variable | Default |
|---|---|
| `AGENT_SEMANTIC_CANDIDATE_THRESHOLD` | `0.85` (cosine similarity, candidates only) |
| `AGENT_SEMANTIC_CANDIDATE_LIMIT` | `5` |
| `AGENT_OFFICIAL_PAGE_FETCH_ENABLED` | `true` |
| `AGENT_OFFICIAL_PAGE_TIMEOUT_SECONDS` | `10` |
| `AGENT_OFFICIAL_PAGE_MAX_BYTES` | `1500000` |
| `AGENT_RSS_FEEDS` | comma-separated feed URLs, polled every 30 min |

## Publishing

| Variable | Default | Notes |
|---|---|---|
| `AGENT_PUBLISHERS` | `manual` | comma list of `mock`, `manual`, `telegram` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_PUBLISH_CHAT_ID` | — | required for `telegram`; the bot must be an admin of the channel |

## WhatsApp

See WHATSAPP.md: `WHATSAPP_CONNECTOR`, `ALLOWED_WHATSAPP_GROUP_IDS`,
`ALLOWED_WHATSAPP_SENDER_IDS`, `WHATSAPP_BOT_IDS`,
`WHATSAPP_BRIDGE_HMAC_SECRET`, `WHATSAPP_CLOUD_APP_SECRET`,
`WHATSAPP_CLOUD_VERIFY_TOKEN`.

## Example: zero-cost setup

```
AGENT_LLM_PROVIDER=openrouter
OPENROUTER_BASE_URL=http://localhost:11434/v1   # self-hosted Ollama
OPENROUTER_API_KEY=ollama                        # any non-empty value for local servers
AGENT_LLM_MODEL=<a model you pulled locally>
AGENT_WRITER_MODE=template
AGENT_PUBLISHERS=manual
WHATSAPP_CONNECTOR=disabled                      # use volunteer submissions first
```
