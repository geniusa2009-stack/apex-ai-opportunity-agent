# APEX AI Opportunity Agent — Architecture

The agent turns raw opportunity messages (WhatsApp, volunteers, Telegram,
websites, RSS) into reviewed, **human-approved** APEX posts. It is a
module of the existing FastAPI backend (a modular monolith), not a new
service. It reuses the existing `sources`, `opportunities`, D6
`claims`/`evidence`, change history and moderation activity log.

```
                 ┌──────────── connectors (app/agent/connectors) ────────────┐
 WhatsApp ──────►│ cloud_api / bridge / mock  → allowlist + loop protection │
 Volunteer API ─►│ POST /agent/submissions                                    │
 RSS / Atom ────►│ RSSConnector (defusedxml)                                  │
 Telegram/FB ───►│ LegacyAdapterConnector (existing source adapters)          │
                 └───────────────────────────┬────────────────────────────────┘
                                             │  IngestionEvent (one internal shape)
                                             ▼
                         ingestion.ingest_event()  ─ source approval, idempotency,
                                             │       exact content-hash duplicates
                          SourceMessage + PipelineJob (database, never memory)
                                             │
         Celery beat (every minute) / POST /admin/agent/run  ── claim with lease
                                             ▼
   PROCESSING ─► EXTRACTING ─► VERIFYING ─► WRITING ─► REVIEWING ─► WAITING_APPROVAL
   triage        1 LLM call    page fetch    template    quality gate       (human)
   exact dedup   self-correct  D6 claims     or LLM +    (+ optional            │
   semantic      full dedup    red flags     fact guard   LLM review)           ▼
   candidates    decision                    RAG examples                  PUBLISHING ─► COMPLETED
                                                                           idempotent      │
                                                                           publishers      ▼
                                                                                    PostPerformance
                                                                                    → RAG ranking
```

## Modules (`app/agent/`)

| Module | Responsibility | AI? |
|---|---|---|
| `connectors/` | Produce `IngestionEvent`s; WhatsApp gate (allowlists, bot-loop) | no |
| `ingestion.py` | Store `SourceMessage`, idempotency, exact duplicates, enqueue job | no |
| `normalization.py`, `dates.py` | URL/text/title normalization, hashes, date parsing | no |
| `dedup.py` | Fingerprint layers + candidate generators + pure `compare()` | no (embeddings only propose) |
| `llm/` | `LLMProvider` + adapters (Gemini, OpenRouter/OpenAI-compatible), structured output with bounded self-correction, embeddings | — |
| `extraction.py` | Rule triage; `ExtractedOpportunity` schema with grounding | yes (1 call) |
| `verification.py` | Official page fetch + D6 claims/evidence + red flags | no |
| `writer.py` | Template writer, optional LLM writer, deterministic fact guard | optional |
| `review.py` | Deterministic quality gate (+ optional LLM review, stricter only) | optional |
| `approval.py` | Human approve/reject/revise/continue/discard | no |
| `publishing.py` | `PublisherConnector`, idempotent `Publication` lifecycle | no |
| `rag.py`, `performance.py` | Historical examples ranked by similarity/performance/recency | no |
| `jobs.py` | Leases, transitions, backoff, dead letter, requeue | no |
| `pipeline.py` | Orchestrates the stages for one job, checkpoint per stage | — |
| `reputation.py` | Source trust → priority / evidence requirement (never approval) | no |
| `observability.py`, `metrics.py` | Structured redacted logs, DB-computed metrics | no |
| `config_checks.py` | Fatal startup checks + readiness helpers | no |

## Data model (migration `0068_ai_opportunity_agent`)

```
sources (+trust_score, +access_status)
  └─< source_messages >─┬─ opportunity_source_messages >─ opportunities (existing, unchanged)
                        │                                     ├─ claims/evidence (existing D6)
                        │                                     ├─ agent_opportunity_facts (1:1)
                        │                                     └─< opportunity_fingerprints
                        └─< pipeline_jobs ─< pipeline_job_events
                                   └─ agent_post_drafts ─< publications ─< post_performance
```

* The `opportunities` table is **not altered** (dozens of raw-SQL test
  fixtures depend on its exact shape); the agent's extra facts live in
  `agent_opportunity_facts`.
* Agent-created opportunities are always `review_status=pending_review`,
  `is_verified=false` until a human approves.
* Updates to an existing (possibly live) opportunity are stored as a
  **proposal** and applied — with change history — only on approval.

## Design rules

1. **Deterministic first.** Normalization, hashing, dedup decisions, date
   parsing, schema validation, retries, job states, authorization,
   reputation, publishing state and idempotency are plain code.
2. **AI only where language understanding is needed:** extraction (always),
   writing and reviewing (optional). Chatter and obvious re-posts cost
   0 calls; a normal message costs 1.
3. **Facts > style.** URLs and dates in an extraction must appear in the
   source message; every post passes a fact guard (links, dates, numbers).
4. **Trust ≠ approval.** Trust changes priority and how much evidence is
   demanded, never whether verification or a human runs.
5. **At-most-once publishing.** Unique idempotency key; `PUBLISHING`
   committed before the external call; uncertain delivery is never re-sent
   automatically.
6. **Replaceable edges.** LLM provider, embedding provider, WhatsApp
   connector, publishers, page fetcher, queue trigger and hosting are all
   behind small interfaces and configuration.

## What was reused instead of rebuilt

`Source`, `Opportunity`, `OpportunityReviewStatus`, the D6
`evidence_verification` service (claims, evidence, deterministic
decision), `change_history_service`, `moderation_log_service`, the role
tiers (`RequireModerationAccess`...), the existing source adapters
(Telegram/Facebook), Celery/Redis, the taxonomy normalizers.
