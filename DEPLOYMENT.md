# Deployment

The backend is one image with two roles: the API (`uvicorn`) and the worker
(`celery worker` + `celery beat`). Postgres (+pgvector) and Redis are
external services. Nothing assumes a particular host; any platform that runs
a container with environment variables works.

## Image

```bash
docker build -t apex-backend .
```

* `.dockerignore` keeps `.env*` (except `.env.example`), `.venv`, `.git`,
  caches and local reports out of the build context. Before this change
  `COPY . .` copied `.env` and `.env.bak_realenv` into the image — **if an
  image was ever built and pushed from the old Dockerfile, rotate the keys it
  contained.**
* Runs as the unprivileged `apex` user.
* `HEALTHCHECK` → `GET /health` (liveness, no dependencies).

## Processes

| Role | Command |
|---|---|
| API | default `CMD` (`uvicorn ... --timeout-graceful-shutdown 20`) |
| Worker | `celery -A app.core.celery_app worker --loglevel=info` |
| Scheduler | `celery -A app.core.celery_app beat --loglevel=info` (exactly one instance) |

Probes: liveness `GET /health`; readiness `GET /ready` (DB, Redis, live
pgvector dimension; 503 with named failing checks, no secrets).

## Graceful shutdown and restarts

* API: uvicorn stops accepting connections on SIGTERM and drains requests
  for up to 20 s.
* Worker: Celery's warm shutdown finishes the running task. If the process is
  killed anyway, nothing is lost: the job stays in `pipeline_jobs` with a
  lease and is resumed at its current stage by the next run after
  `AGENT_JOB_LEASE_SECONDS`.
* Publishing is at-most-once: an interrupted send is never re-sent blindly
  (status `unknown` → a human confirms).

## Release steps

1. Set environment variables (ENVIRONMENT.md). In production:
   `ENVIRONMENT=production` (the mock WhatsApp connector is then refused).
2. `alembic upgrade head` (see SETUP.md for the pre-existing fresh-database
   issues). New in this release: `0068_ai_opportunity_agent` (additive),
   `0069_fix_users_search_trigger`, `0070_opp_moderation_enums`.
3. Start API, worker, beat. Check `/ready`.
4. Optional: `GET /api/v1/admin/agent/metrics` after the first messages.

## Hosting notes (0 EGP)

* Free tiers change and sleep policies differ; nothing here depends on one.
  Do **not** add uptime pingers to defeat sleep policies — jobs simply run
  when the worker is up (the database queue keeps them).
* Without a separate worker, `POST /api/v1/admin/agent/run` (Senior
  Moderator+) advances due jobs from the API process.
* Supabase Postgres already has pgvector; Redis can be any managed Redis.
