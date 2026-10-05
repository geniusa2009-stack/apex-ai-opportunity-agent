# Testing — AI Opportunity Agent

## Suites

| File | Covers |
|---|---|
| `tests/test_agent_config_and_security.py` | .dockerignore really excludes secrets, non-root Dockerfile, startup config checks, `/health`, `/ready` |
| `tests/test_agent_ingestion_and_jobs.py` | SourceMessage, idempotent delivery, content-hash duplicates, source approval, bot messages, malformed events, leases, backoff, dead letter, NEEDS_REVIEW/FAILED routing, restart recovery, requeue, audit trail, reputation bounds |
| `tests/test_agent_normalization_dedup.py` | URL/text/title normalization, dates (EN/AR), all dedup layers, NEW / DUPLICATE / UPDATE_EXISTING / POSSIBLE_DUPLICATE, similarity never decides alone, pre-extraction check |
| `tests/test_agent_llm_and_extraction.py` | triage, JSON parsing, self-correction (malformed JSON, schema failure, invented link/date), bounded retries, fallback, OpenRouter and Gemini adapters (mocked transports), embedding dimension guard |
| `tests/test_agent_llm_router.py` | free-only routing: chain order, bounded 429/503 retries, retry-after, cooldowns, 404/auth/cost → next provider, truncation → next model, schema failures never switch provider, all-fail categories, Gemini allowlist, OpenRouter zero-price checks (fail-closed catalog, `max_price=0`, reported cost), Ollama local-only |
| `tests/test_agent_llm_cloud_providers.py` | Groq and Cloudflare providers: request shape, catalog gates (fail closed), paid-only / unlisted / inactive / deprecated models refused, free-plan confirmation required, 429/5xx, quota exhaustion → cooldown to 00:00 UTC, invalid requests not hidden, truncation, JSON-mode support, reasoning stripped; Ollama unreachable → fast skip; full chain order; missing credentials |
| `tests/test_agent_verification.py` | official page confirmation, contradicting deadline, closed applications, dead link, unreachable page, weak/strong source, expiry, redirects, shorteners, scam signals, SSRF guard, size/timeout limits |
| `tests/test_agent_writer_review_rag.py` | template writer, fact guard, LLM writer fallback, examples cannot inject facts, quality gate, LLM reviewer only stricter, performance score, RAG ranking |
| `tests/test_agent_pipeline_e2e.py` | **end-to-end** WhatsApp → … → approval → publisher → performance; webhook rules; bridge/Cloud API signatures; duplicate/update/reject/revise/contradiction/expiry branches; LLM retry → dead letter; schema failure; crash recovery without repeating the LLM call; publishing retry, uncertain delivery, idempotency, crash mid-publish, manual and Telegram publishers; volunteer submissions; authorization |
| `tests/test_agent_feeds.py` | RSS/Atom parsing, XML entity attacks refused, legacy adapter bridge |
| `tests/test_agent_postgres_integration.py` | **real Postgres + pgvector**: full pipeline, SKIP LOCKED claims, DB-enforced idempotency and attempt bounds, pgvector candidate query (skipped unless `APEX_TEST_DATABASE_URL` is set) |

Everything external is faked (LLM, pages, WhatsApp, publishers): no keys,
no network, deterministic.

## Run

```bash
# required settings with dummy values (nothing real is contacted)
export SUPABASE_JWT_SECRET=test DATABASE_URL=postgresql+asyncpg://t:t@localhost:1/t \
  SYNC_DATABASE_URL=postgresql://t:t@localhost:1/t REDIS_URL=redis://localhost:1/0 \
  CELERY_BROKER_URL=redis://localhost:1/0 CELERY_RESULT_BACKEND=redis://localhost:1/1 \
  SUPABASE_URL=http://localhost SUPABASE_SERVICE_ROLE_KEY=test

pytest -q tests/test_agent_*.py            # agent suites (~210 tests, ~20 s)
pytest -q tests                            # full regression
```

Postgres integration (disposable DB migrated to head — it gets TRUNCATED):

```bash
APEX_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/apex_test \
  pytest -q tests/test_agent_postgres_integration.py
```

Live HTTP proof (real uvicorn + real Postgres + real JWT, fakes only for
LLM/page/publisher): see the docstring of `scripts/agent_live_proof.py`.

## Results at hand-over (5 Oct 2026)

* Agent suites: 206 passed, 4 skipped (the Postgres ones without the env var).
* Postgres integration on Postgres 16.2 + pgvector 0.6.2: 4/4.
* Live HTTP proof: 20/20.
* Full regression: **1859 passed**; 7 failed + 17 errors — the **identical**
  set that fails on the untouched baseline (test_dashboards, test_live_sessions,
  test_live_session_reminders, test_shifts, test_shifts_debug_tmp,
  test_student_videos, test_user_search); none involve the agent.
* Migrations: 0068 → 0070 upgrade/downgrade/upgrade on real Postgres; zero
  model/schema drift for the agent tables.
* `ruff check` and `mypy` (0 issues) on all agent modules.
* Docker: image built from this Dockerfile (in the sandbox, only the egress
  proxy's CA had to be added for pip). Verified inside the image: runs as
  `apex` (uid 999); `.env`, `.env.bak_realenv`, `.venv`, `.git` absent even
  though they existed in the build context; HEALTHCHECK `healthy`;
  `docker stop` exits 0 in ~1 s; `EMBEDDING_DIMENSION=768` makes the
  container refuse to start with a clear message.
