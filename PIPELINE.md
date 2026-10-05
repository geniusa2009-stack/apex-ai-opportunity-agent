# Pipeline — stages, states, retries

## Job states

```
QUEUED → PROCESSING → EXTRACTING → VERIFYING → WRITING → REVIEWING → WAITING_APPROVAL → PUBLISHING → COMPLETED
                                                    ▲          │
                                                    └──────────┘ (bounded revision rounds)
any work stage ──(validation/data error, blocked verification, reviewer REJECT)──► NEEDS_REVIEW (human)
any work stage ──(retryable error, attempts < max)──► same stage, next_retry_at = now + backoff
any work stage ──(retryable error, attempts = max)──► DEAD_LETTER
any work stage ──(auth/config error)──► FAILED
```

Allowed transitions are an explicit table in `app/agent/jobs.py`; anything
else raises `InvalidTransitionError`. Every transition is appended to
`pipeline_job_events` (who/what/why/duration).

## Stages

| Stage | What happens | Writes | LLM |
|---|---|---|---|
| PROCESSING | rule triage (chatter → done), pre-extraction exact dedup (content hash / specific URL with no new dates → DUPLICATE) | message status | 0 |
| EXTRACTING | extraction with self-correction (skipped if already stored), full dedup decision, create opportunity or stage update proposal | `source_messages.extraction`, opportunity, facts, fingerprints, links | 1 |
| VERIFYING | official page fetch + matching, D6 claims/evidence, red flags, level; expired → done, blocking → NEEDS_REVIEW | claims, evidence, verification report | 0 |
| WRITING | RAG examples (LLM mode), writer, fact guard | `agent_post_drafts` | 0 / 1 |
| REVIEWING | quality gate (+ optional LLM review); revise (LLM mode, bounded) or hand to human | draft decision + reasons | 0 / 1 |
| WAITING_APPROVAL | parked; human approves / rejects / requests revision | — | — |
| PUBLISHING | each configured publisher, idempotently | `publications` | 0 |

Each stage commits its results before the job moves on (checkpoint) and
first looks for its own previous results, so re-running a stage after a
crash does not repeat work (e.g. no second LLM extraction).

## Deduplication outcomes

| Outcome | Effect |
|---|---|
| `NEW` | new opportunity (pending review) |
| `DUPLICATE` | message linked to the existing opportunity, job completes (no draft) |
| `UPDATE_EXISTING` | proposal of changed fields; new draft; applied only on approval |
| `POSSIBLE_DUPLICATE` | new opportunity + draft, flagged "possible duplicate of X" for the human |

Similarity (embeddings or lexical) can only produce candidates and at most
`POSSIBLE_DUPLICATE`. Decisions use organization, title, deadline,
application/official URL, type and edition year (`dedup.compare`).

## Retries

* Backoff: `base * 2^(attempt-1)`, capped (`AGENT_JOB_BACKOFF_*`), no jitter
  (reproducible). Attempts are capped by `AGENT_JOB_MAX_ATTEMPTS` (1–20).
* Error categories (`app/agent/errors.py`): TRANSIENT, RATE_LIMIT,
  EXTERNAL_SERVICE, SYSTEM → retry; VALIDATION, DATA → NEEDS_REVIEW;
  AUTHENTICATION (incl. missing config) → FAILED.
* LLM output validation has its own bounded loop
  (`AGENT_LLM_MAX_VALIDATION_RETRIES`, 0–5) inside one attempt.
* A failed stage is rolled back before the retry policy runs.

## Restart safety

Workers claim jobs with a lease (`locked_until`); on Postgres the claim uses
`FOR UPDATE SKIP LOCKED`. A crashed worker's job becomes claimable once the
lease expires and resumes at its current stage. Celery is only a trigger:
lost or duplicate triggers are harmless.

## Human operations

| Action | Endpoint |
|---|---|
| list drafts waiting | `GET /api/v1/admin/agent/approvals` |
| approve (optionally edited text) | `POST /api/v1/admin/agent/drafts/{id}/approve` |
| reject (optionally "source was inaccurate") | `POST .../drafts/{id}/reject` |
| request a revision | `POST .../drafts/{id}/request-revision` |
| let a NEEDS_REVIEW job continue / discard it | `POST .../jobs/{id}/continue`, `.../discard` |
| requeue FAILED / DEAD_LETTER | `POST .../jobs/{id}/requeue` |
| confirm a manual post / resolve UNKNOWN | `POST .../publications/{id}/confirm` |
| add performance numbers | `POST .../publications/{id}/performance` |
| metrics | `GET .../metrics?days=30` |
| sources (trust, approve/revoke) | `GET/PATCH .../sources` (PATCH is Owner-only) |

All are Moderator+ unless noted; every decision is logged to the existing
moderation activity log.

## Metrics

`opportunities_received`, `opportunities_rejected`, `opportunities_verified`,
`duplicates_detected`, `ai_failures`, `verification_failures`,
`publications_successful`, `publications_failed`, `average_processing_time`
(+ `jobs_by_status`) — computed from the database, so they are correct across
instances and restarts.

## Troubleshooting

| Symptom | Look at | Typical cause / fix |
|---|---|---|
| job stuck in `extracting` with `next_retry_at` | job detail `last_error_code` | LLM rate limit / outage; it retries automatically |
| job `failed`, `LLM_NOT_CONFIGURED` | config | set `AGENT_LLM_PROVIDER` + `AGENT_LLM_MODEL` + key, then requeue |
| job `needs_review`, `LLM_SCHEMA_VALIDATION_FAILED` | job events | model could not produce grounded JSON; continue/discard by hand or try another model |
| job `needs_review` at `verification_blocked` | `payload.verification` | contradiction / fee request / weak source without link; a human decides |
| publication `unknown` | publications list | delivery uncertain; check the channel, then `confirm` |
| `dead_letter` | `last_error` | fix the cause, then `requeue` |
| app refuses to start | startup log `config fatal` | the named setting is wrong (e.g. embedding dimension) |
