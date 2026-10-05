# Setup (local development)

## 1. Install

```bash
python3.10 -m venv .venv            # 3.10+ (the Docker image uses 3.12)
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install Mako          # alembic needs it; it was missing from the existing Mac .venv
```

## 2. Configure

```bash
cp .env.example .env                # then fill in real values -- .env is git- and docker-ignored
```

Minimum to boot: `SUPABASE_JWT_SECRET`, `DATABASE_URL`, `SYNC_DATABASE_URL`,
`REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`. The agent works with its defaults (no LLM,
template writer, manual publisher, WhatsApp disabled); see ENVIRONMENT.md
to turn things on.

## 3. Database (Postgres 16 + pgvector)

`docker compose up -d db redis` starts both (see docker-compose.yml), or use
Supabase.

```bash
.venv/bin/alembic upgrade head
```

### Known pre-existing migration issues (fresh databases only)

These exist in migrations written before the agent and are **not** caused by
it. Existing databases that already ran those migrations are unaffected.

1. **`alembic_version.version_num` is VARCHAR(32)** but several revision ids
   are longer (e.g. `0050_opportunity_manual_create_action`). On a brand-new
   database create the table first:
   ```sql
   CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY);
   ```
2. **0040 and 0047 create the `chat_report_reason` enum twice** (the column
   definition re-creates the type), so `upgrade head` stops at 0040 on a fresh
   database. The fix is adding `create_type=False` to those two
   `postgresql.ENUM(...)` definitions. It was NOT applied here because the
   brief forbids rewriting historical migrations — it is a one-line decision
   for the team (it does not change anything for databases that already ran
   them).

Two other pre-existing bugs were fixed with NEW migrations:
* `0069` — the `users` search-vector trigger referenced `full_name` instead
  of `NEW.full_name`, so every user INSERT failed on a migrated database.
* `0070` — `opportunity_approve` / `opportunity_reject` / `opportunity` were
  missing from the native enums, so opportunity approve/reject failed on
  Postgres.

## 4. Run

```bash
.venv/bin/uvicorn app.main:app --reload                       # API
.venv/bin/celery -A app.core.celery_app worker --loglevel=info  # worker
.venv/bin/celery -A app.core.celery_app beat --loglevel=info    # schedules (agent every minute)
```

No Celery? `POST /api/v1/admin/agent/run` (Senior Moderator+) advances due
jobs inside the request.

## 5. Try the agent without any API key

```bash
export WHATSAPP_CONNECTOR=mock ALLOWED_WHATSAPP_GROUP_IDS=120363@g.us AGENT_PUBLISHERS=mock
curl -X POST localhost:8000/api/v1/agent/whatsapp/webhook -H 'Content-Type: application/json' \
  -d '{"messages":[{"message_id":"m1","group_id":"120363@g.us","sender_id":"201000000001","text":"منحة ... https://example.org/apply"}]}'
```

Without an LLM the job stops at extraction with `LLM_NOT_CONFIGURED`
(`failed`); set `AGENT_LLM_PROVIDER`/`AGENT_LLM_MODEL`/key and requeue it.
`scripts/agent_live_proof.py` runs the whole flow with fakes (TESTING.md).
