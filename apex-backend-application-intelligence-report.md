# APEX Backend — Application Intelligence Report

**Date:** 2026-09-25
**Baseline:** `e4e93a7` (verified clean, matches the task's stated "CURRENT BASELINE")
**Scope:** Application Intelligence — the backend foundation connecting APEX's Opportunity Intelligence to a user's real application journey (Discover → Understand → Decide → Prepare → **Apply/Submit** → Track).

---

## 1. Repository Audit

Before writing any code, the following were read in full or grepped and confirmed:

- `app/models/application_tracker.py`, `app/schemas/application_tracker.py`, `app/api/v1/endpoints/application_tracker.py`, `app/services/application_tracker.py` — the pre-existing, Kanban-style `ApplicationTracker` system, keyed by free-text `scholarship_name`/`institution`.
- `app/models/claim.py` and `app/services/evidence_verification.py::ALLOWED_CLAIM_TYPES` — confirmed no "requirements" claim type exists anywhere in the D6 verification pipeline.
- `app/models/opportunity.py` — confirmed only `eligibility_criteria` and `funding_details` (both free `Text`) exist as candidate requirement sources; no structured field of any kind.
- `app/services/deadline_intelligence.py` — a pre-existing, deterministic, standalone `classify_deadline_text()` / `DeadlineState` enum, explicitly documented as "not yet wired into a real column."
- `app/services/deadline_alerts.py` / `app/models/deadline_alert.py` — a separate, pre-existing, Gemini-driven milestone/reminder system scoped to `tracker_id`, distinct in purpose from this phase's guidance-integration work.
- `app/models/opportunity_change_history.py` / `app/services/change_history_service.py` — the existing, already-integrated (via the prior Opportunity Interactions phase) change-tracking table.
- `app/services/guidance_service.py` — the existing, deterministic, read-time Proactive Guidance engine from the prior phase.
- `app/models/enums.py` — confirmed `TrackerStatus` (INTERESTED, PREPARING_DOCS, APPLIED, INTERVIEWING, ACCEPTED, REJECTED) and `NextActionType` already exist and are in active use.
- `app/services/opportunity_state_machine.py` — the existing transition-table pattern for `OpportunityLifecycleState`, used as the direct structural precedent for this phase's new application state machine.
- `app/models/user_history.py` — confirmed no product-level link exists between any "document" concept and `ApplicationTracker`.
- `tests/` — grepped for every existing reference to `application_tracker`/`ApplicationTracker`/`/tracker`; only `tests/test_user_intelligence.py` touches it, and only via a raw-SQL fixture table, never the service or endpoint layer.

**Key finding:** `ApplicationTracker` had **no `opportunity_id`** and **no transition validation** on its status field — `PATCH /tracker/{id}/status` accepted any string that passed Pydantic's `Literal` check, including `"applied"`, with zero business-rule enforcement. This is the exact, real gap the task's Part U anticipated.

## 2. Existing Application Architecture (before this phase)

`ApplicationTracker` (table `application_trackers`): `id`, `user_id`, `scholarship_name` (required free text), `institution`, `status` (plain `String(30)`), `deadline`, `notes`, `ai_todo_list` (JSONB, Gemini-generated 4–6 prep tasks), `created_at`. Router at `/tracker`: `POST /`, `GET /` (list), `PATCH /{id}/status` (no validation), `PATCH /{id}/todo/{task_id}`, `DELETE /{id}`. No `GET /{id}` existed. No link to the `Opportunity` catalog existed — a tracker was pure user-entered text with an AI-generated checklist bolted on.

## 3. New / Extended Data Model

No new "Application" model was created — `ApplicationTracker` itself was extended, per Part A/T's explicit "reuse, don't duplicate" instruction:

- `opportunity_id: UUID | None` — nullable FK to `opportunities.id`, `ondelete="SET NULL"` (deleting a catalog opportunity must never delete a user's own tracked record), indexed.
- `submitted_at: datetime | None` — nullable, stamped only by the one controlled submission pathway.
- `opportunity: Opportunity | None` relationship, `lazy="selectin"`.
- Partial unique index `uq_application_trackers_user_opportunity` on `(user_id, opportunity_id) WHERE opportunity_id IS NOT NULL` — the database-level half of Part T's duplicate-application protection (off-catalog trackers, with no `opportunity_id`, are unconstrained, since two freeform entries can legitimately coexist).

One new model, `ApplicationRequirementCompletion` (table `application_requirement_completions`): `id`, `tracker_id` (FK, `ondelete="CASCADE"`), `requirement_key` (stable slug, e.g. `cv_resume`), `is_completed`, `completed_at`, `created_at`. Kept structurally separate from the pre-existing `ai_todo_list` JSONB column — the two represent genuinely different checklist concepts (AI-suggested prep tasks vs. deterministic, opportunity-derived, user-confirmed requirements) and mixing them would blur Part E's own "distinguish USER_CONFIRMED_COMPLETE from SYSTEM_INFERRED" requirement.

Two new enum members were added to reuse the existing vocabulary rather than invent a parallel one: `TrackerStatus.WITHDRAWN` (a real, final "user explicitly stopped pursuing this" state, distinct from `REJECTED`, needed for Part O/M) and `NextActionType.SUBMIT_APPLICATION`.

## 4. Application Lifecycle

A new module, `app/services/application_state_machine.py`, mirrors `opportunity_state_machine.py`'s own transition-table pattern:

```
INTERESTED      -> PREPARING_DOCS, WITHDRAWN
PREPARING_DOCS  -> INTERESTED, WITHDRAWN
INTERVIEWING    -> WITHDRAWN
APPLIED         -> INTERVIEWING, ACCEPTED, REJECTED, WITHDRAWN
ACCEPTED        -> (terminal)
REJECTED        -> (terminal)
WITHDRAWN       -> (terminal)
```

`APPLIED` is **structurally unreachable** via the generic transition path (`validate_generic_transition` explicitly rejects any proposed transition to `APPLIED`, not merely omits it from the table). The only way to reach `APPLIED` is `application_tracker.submit_tracker()`, which calls the separate `validate_submission_transition()` (allowed only from `INTERESTED`, `PREPARING_DOCS`, `INTERVIEWING`) and stamps `submitted_at` in the same transaction as the status change. `PATCH /tracker/{id}/status` with `new_status="applied"` now returns `422 Unprocessable Entity` — this is the literal scenario Part U's own worked example describes, and it is now rejected by construction, not by convention.

## 5. Requirement Model

`app/services/application_requirements.py` — a pure, deterministic, keyword-based extraction function, `extract_requirements(opportunity) -> list[RequirementItem]`. This was the central design decision of the phase: since no structured "requirements" data exists anywhere in the schema or the D6 claim-verification pipeline, and the task explicitly forbids letting an LLM invent requirements, the only fabrication-free option was to match a small, explicit set of category keywords (CV/résumé, personal statement, essay, recommendation letter, transcript, portfolio, language test, standardized test score, financial document, passport/travel document) against the opportunity's own three already-verified free-text fields (`eligibility_criteria`, `funding_details`, `description`). Every match carries the literal matched sentence as `evidence_snippet` and which field it came from — full traceability, no invented data. Status (`required` / `conditional` / `optional`) is only reassigned away from the `required` default when the matched sentence itself carries explicit hedging language ("may be required", "if applicable") or explicit optional language ("optional", "not required but recommended"). No confidence score is attached anywhere in this module. An opportunity with no matching text produces an honestly empty list — never a generic fallback like "you'll probably need a CV."

## 6. Checklist Architecture

`GET /tracker/{id}/requirements` returns each detected requirement plus its completion state, sourced from `ApplicationRequirementCompletion`. Completion is exclusively user-driven: `POST /tracker/{id}/requirements/{key}/complete` and `.../uncomplete` are the only writers of that table — nothing else in the codebase (including any future document-upload feature) can mark a requirement complete as a side effect. `complete_requirement` upserts idempotently (calling it twice is a no-op, matching `bookmarks.py`'s established idempotency posture); `uncomplete_requirement` deletes the row entirely rather than flipping a flag, so a re-derived requirements projection and the completions table can never disagree about whether a completion record exists. The pre-existing AI-generated `ai_todo_list` (its own independent toggle route, `PATCH /{id}/todo/{task_id}`) is left completely unmodified and is surfaced alongside, not merged into, the new requirements list.

## 7. Deadline Integration

No new deadline parser was written. `application_intelligence.py` calls the pre-existing `deadline_intelligence.classify_deadline_text()` directly against the opportunity's own verified text plus the tracker's stored `deadline`, producing one of the existing `DeadlineState` values (EXACT, DATE_ONLY, ROLLING, ONGOING, NO_DEADLINE_STATED, CLOSED, EXTENDED, UNKNOWN). For an off-catalog tracker (no linked opportunity), only the stored `deadline` value is classified — the user's own free-text `notes`/`scholarship_name` is never treated as verified evidence, so `UNKNOWN` is the honest answer when nothing is stored.

## 8. Guidance Integration

`app/services/guidance_service.py` was extended, not replaced or duplicated. Two new generator functions were added to the existing `build_guidance()` pipeline:

- `_application_missing_requirements_guidance` — for every active (non-terminal), opportunity-linked tracker, surfaces each still-missing `required` item, with the reason quoting the opportunity's own evidence sentence.
- `_application_change_impact_guidance` — Part I's opportunity-change-impact pipeline, implemented as a read-time query (the same pattern the prior phase established for bookmarks) rather than a live event hook, scoped to active, opportunity-linked trackers and the `deadline` field.

Both are wired directly into `build_guidance`'s existing `candidate_items` list — `GET /users/me/guidance` (an unmodified route from the prior phase) now automatically surfaces application-related guidance for every user. No second guidance/notification engine was created; Part H's "integrate with existing reminder infra, don't build a new one" is satisfied by treating this guidance pipeline itself as the integration point, since it is already the codebase's real, live proactive-surfacing mechanism.

## 9. Opportunity-Change Impact

Implemented via `_application_change_impact_guidance` (above) and mirrored in the read-time `ApplicationIntelligenceSnapshot.recent_opportunity_changes` field, which queries `OpportunityChangeHistory` for the linked opportunity within a 7-day window. Per Part I's explicit instruction, no automatic mutation of `ApplicationTracker.status` occurs when an opportunity changes — a changed deadline surfaces as guidance and in the snapshot; it never silently flips a tracker to a different state.

## 10. API Changes

All changes extend the existing `/tracker` router (Part K's "extend rather than duplicate... fit existing project conventions" — a new `/applications` prefix was deliberately not introduced):

| Method | Path | Status |
|---|---|---|
| POST | `/tracker/` | modified — accepts optional `opportunity_id`, prefills name/deadline from it, enforces duplicate protection |
| GET | `/tracker/` | unchanged |
| GET | `/tracker/{id}` | **new** — was genuinely missing |
| PATCH | `/tracker/{id}/status` | modified — now runs through the transition table; rejects `applied` (422) |
| POST | `/tracker/{id}/submit` | **new** — the one controlled submission pathway |
| GET | `/tracker/{id}/requirements` | **new** |
| POST | `/tracker/{id}/requirements/{key}/complete` | **new** |
| POST | `/tracker/{id}/requirements/{key}/uncomplete` | **new** |
| GET | `/tracker/{id}/intelligence` | **new** — the read-only snapshot |
| PATCH | `/tracker/{id}/todo/{task_id}` | unchanged |
| DELETE | `/tracker/{id}` | unchanged |

Confirmed via `app.main.app.openapi()`: 247 total API paths (up from 242), all 9 `/tracker/*` paths present with the correct HTTP methods, no route collisions.

## 11. Security / IDOR

Every new and modified route uses the same `get_owned_tracker` / `_get_owned_tracker` lookup already established by the pre-existing router (raises `TrackerNotFoundError`, mapped to a flat 404 — deliberately not distinguishing "doesn't exist" from "not yours," the same posture as `history.py`). `complete_requirement`, `uncomplete_requirement`, and `submit_tracker` each perform this ownership check before touching any row. `TestUserIsolation` (5 tests) directly verifies: another user cannot read, update the status of, submit, or complete a requirement on someone else's tracker, and guidance built for one user never includes another user's application data.

## 12. Performance

`GET /tracker`, `GET /tracker/{id}`, and `GET /tracker/{id}/intelligence` make zero LLM calls and stay fully deterministic, per Part R. The new `opportunity_id` foreign key and the partial unique index are both indexed. `extract_requirements()` is a pure, in-memory function with no DB or network access. Guidance's two new generators query only opportunity-linked, non-terminal trackers (already the caller's own small per-user list), not a full table scan.

## 13. Tests

`tests/test_application_intelligence.py` — 39 new tests, organized to match Part S's checklist: `TestApplicationDomainModel` (5), `TestStateMachine` (8), `TestSubmissionSafety` (4, including a structural source-inspection test asserting no external submission integration exists), `TestRequirementsExtraction` (4, including prompt-injection resistance), `TestChecklistAndSnapshot` (6), `TestDeadlineIntegration` (3), `TestGuidanceIntegration` (4, including the exact opportunity-change-impact scenario), `TestUserIsolation` (5). All exercise the real ORM models and real service/endpoint code against an in-memory SQLite fixture, following the established convention in `test_user_intelligence.py`. The one Gemini-backed call inside `create_tracker` (`_generate_todo_tasks`, pre-existing and out of scope for this phase) is mocked at the same function boundary `test_aggregator.py` already established for its own Gemini call — everything else in the code path under test runs for real.

**Result: 39/39 passed.**

Final validation also ran:
- Combined import smoke test across every new/modified module plus `app.main.app.openapi()` — passed, 247 total paths.
- `tests/test_user_intelligence.py` (27) and `tests/test_personalized_intelligence.py` (43) — re-run in full, **70/70 passed**, confirming zero regression from the `ApplicationTracker` schema change and the `guidance_service.py` extension.
- Full existing suite, run in batches (~1,450 tests total across all files): all passed except the following, each independently re-confirmed as pre-existing and environmental, not caused by this diff:
  - `test_dashboards.py::test_aggregates_gamification_notifications_videos_and_opportunities` — requires a live Redis connection, unavailable in this sandbox.
  - `test_live_session_reminders.py` (6 errors) and `test_student_videos.py` (11 errors) — SQLite cannot compile the `JSONB` column type used by production Postgres; a standing, disclosed test-environment limitation.
  - `test_shifts.py` (2 failures) — a hardcoded-past `NOW` fixture date has drifted further out of range as real time has advanced.
  - `test_user_search.py::test_requires_authentication` — a pre-existing 403/401 status-code mismatch, unrelated to this phase.
  - One test in `test_live_sessions.py` involving concurrent WebSocket connections hangs in this sandbox (a known, pre-existing environment limitation) and was excluded from the run rather than left to time out.
- `alembic/versions/0064_application_intelligence.py` — syntax-verified (`ast.parse`); revision chain manually confirmed (`down_revision == "0063_opportunity_interactions"`) via `grep`, since `alembic`'s own `ScriptDirectory` tooling cannot run in this sandbox (missing `mako`, a standing, previously-disclosed limitation).
- `git status`/`git diff` reviewed: only the intended 8 modified + 6 new files changed; no secrets, no accidental files, no duplicate models or business logic introduced.

## 14. Known Limitations

- **Application Documents (Part N) deferred entirely.** No product-level link between any existing "document" concept (`UserHistory`, an AI-artifact store unrelated to applications) and `ApplicationTracker`/requirements exists in this codebase. Inventing one would mean fabricating an untested product behavior, which this phase's own audit-first principle forbids. A document upload today has no effect on requirement completion — completion is exclusively the explicit `complete_requirement` call.
- **Guidance-based reminders only — no new notification/email dispatch.** Part H's "integrate with existing reminder infra" is satisfied by extending the read-time guidance pipeline (the codebase's real, live proactive-surfacing mechanism), not by wiring a new live-notification side effect on opportunity change or deadline approach. This matches the prior phase's own precedent for bookmarks.
- **`submit_tracker` does not block on incomplete required items.** The task's spec does not mandate blocking submission, and guidance already surfaces missing items proactively before a user reaches that point; submission remains an informed, unblocked user choice.
- **`DeadlineAlert` (the pre-existing Gemini-driven `.ics`-export milestone system) was not touched or extended.** It is a separate, tracker-scoped concern from this phase's deadline-state classification and guidance-based reminders.

## 15. Deferred Functionality

Per Part W, the following were deliberately **not** built: an AI essay writer or application-writing agent; any real external submission integration; a fake success-prediction score; a generic CRM; expansion of the (still-deferred) course subsystem into the application flow; a generic, standalone document-management system; a second notification engine; a second recommendation engine; any arbitrary or opaque AI confidence/decision score.

---

## Worked Examples

### Example 1 — Discover → Start Application → Requirements → Checklist → Next Actions

A user finds a verified opportunity via `GET /opportunities/{id}` (existing Opportunity Intelligence). Its `eligibility_criteria` reads: *"Applicants must submit a CV and a personal statement of no more than 500 words. A recommendation letter is optional but strongly encouraged."* The user calls `POST /tracker/` with `{"opportunity_id": "<id>"}`. `create_tracker` finds the opportunity published, prefills `scholarship_name` from its real title and `deadline` from its real, stored deadline, generates the pre-existing AI prep checklist, and creates the tracker with `status=INTERESTED`. The user then calls `GET /tracker/{id}/requirements`: `extract_requirements` matches "CV" (source: `eligibility_criteria`, status `required`), "personal statement" (`required`), and "recommendation letter" (status `optional`, because the matched sentence contains "optional but strongly encouraged"). Each carries its literal evidence sentence. The user completes CV and personal statement via `POST .../requirements/{key}/complete`. `GET /tracker/{id}/intelligence` now shows `deadline_state`, the two completed requirements, the still-optional recommendation letter, and (via guidance) no "missing requirement" item remains, since both `required` items are complete.

### Example 2 — Active Application + Opportunity Deadline Changes

The user's tracker above is `PREPARING_DOCS` and linked to the opportunity. A moderator or the aggregation pipeline updates the opportunity's deadline; `change_history_service.record_change_if_meaningful` (pre-existing) writes a new `OpportunityChangeHistory` row with `field_name="deadline"`, the old and new values, and `changed_at=now`. The next time the user calls `GET /users/me/guidance`, `_application_change_impact_guidance` finds this tracker in the "active, opportunity-linked" set, queries `OpportunityChangeHistory` for that opportunity within the last 7 days, finds the deadline-change row, and emits a `GuidanceItem` ("The deadline changed for 1 of your active application(s)") with a `NextActionItem` naming the tracker and quoting the old/new values. Separately, `GET /tracker/{id}/intelligence`'s `recent_opportunity_changes` list independently surfaces the same change row. At no point does the tracker's own `status` change automatically — the user sees the impact and decides what to do next.

### Example 3 — Marking Requirements Complete and Explicitly Submitting

Continuing Example 1: with both required items complete, the user calls `POST /tracker/{id}/submit`. `submit_tracker` looks up the tracker (ownership-checked), calls `validate_submission_transition(current_status="PREPARING_DOCS")` — `PREPARING_DOCS` is in `_SUBMITTABLE_FROM`, so it's allowed — then, in one transaction, sets `status=APPLIED` and `submitted_at=<real UTC timestamp>`, and commits. If the same user later calls `PATCH /tracker/{id}/status` with `{"new_status": "applied"}` directly (bypassing `/submit`), `validate_generic_transition` explicitly rejects any proposed transition to `APPLIED` regardless of current status, returning `422` with the reason "submission must go through the dedicated submit endpoint, not a generic status update." Nowhere in this path does APEX claim to have submitted anything to an external system — `submitted_at` means, and only ever means, "the user told APEX they submitted this," which `TestSubmissionSafety`'s structural test independently confirms by inspecting the module source for the absence of any outbound HTTP call in the submission path.

---

**Philosophy:** APEX understands the user. APEX understands the opportunity. APEX understands where the user is in the journey. APEX helps the user take the next meaningful step. This phase does not turn APEX into a generic application tracker — the application system exists to make APEX's Opportunity Intelligence actionable.
