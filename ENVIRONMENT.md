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
| `AGENT_LLM_CHAIN` | — | Ordered free-only chain, `provider:model,...` (`gemini`, `openrouter`, `ollama`). Overrides the legacy pair below. |
| `AGENT_LLM_RETRIES_PER_PROVIDER` | `1` | Retries on 429/503/timeouts per provider, 0–3. |
| `AGENT_LLM_MAX_RETRY_WAIT_SECONDS` | `20` | Longer server retry-after → provider cooled down, next one tried. |
| `AGENT_LLM_COOLDOWN_SECONDS` / `AGENT_LLM_DISABLE_SECONDS` | `60` / `3600` | Skip time after rate-limit/outage, and after auth / model-gone / cost-policy errors. |
| `AGENT_GEMINI_FREE_MODELS` | free-tier Flash models | Gemini models the agent may use; others are fatal at startup. |
| `AGENT_LLM_PROVIDER` / `AGENT_LLM_MODEL` | `none` | Legacy single provider (used only when the chain is empty). |
| `AGENT_LLM_FALLBACK_PROVIDER` / `_MODEL` | — | Legacy second slot. |
| `AGENT_LLM_TIMEOUT_SECONDS` | `60` | |
| `AGENT_LLM_TEMPERATURE` | `0.1` | |
| `AGENT_LLM_MAX_VALIDATION_RETRIES` | `2` | Self-correction rounds, 0–5. Schema failures never switch provider. |
| `GEMINI_API_KEY` | — | existing setting, reused. Keep its project on the free tier. |
| `OPENROUTER_API_KEY` | — | Only explicit `:free` model ids that are also zero-priced in the live catalog are called (catalog check fail-closed, `max_price=0`, reported cost checked). Free routes share upstream capacity and can return 429 "rate-limited upstream"; list more than one. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | |
| `OPENROUTER_APP_TITLE`, `OPENROUTER_HTTP_REFERER` | | optional attribution headers |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Local/private addresses only. |
| `OLLAMA_API_KEY` | — | Usually empty. |

Routing: each provider gets bounded retries on 429/503/timeouts (honouring
retry-after up to the max wait); auth errors, 404 "model unavailable" and
cost-policy blocks skip straight to the next provider; truncated output or a
context-length 400 tries the next model. Any other HTTP 400/413/422 is an
invalid request (`LLM_INVALID_REQUEST`): it fails clearly and is NOT hidden
by another provider. Known provider-specific 400s (invalid key, model gone,
region not supported) count as provider unavailable and do move on. If all fail, the job runner retries later (then dead letter)
for outages, sends truncation to human review, and marks config/cost errors
FAILED. The router can only move down the configured chain — no paid model is
ever added automatically.

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
AGENT_LLM_CHAIN=gemini:gemini-3.8-flash,gemini:gemini-3.5-flash-lite,ollama:<a model you pulled locally>
AGENT_WRITER_MODE=template
AGENT_PUBLISHERS=manual
WHATSAPP_CONNECTOR=disabled                      # use volunteer submissions first
```
