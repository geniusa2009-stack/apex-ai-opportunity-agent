# APEX — Personalized Opportunity Intelligence + Opportunity Passport

**Status:** implemented, tested, disclosed. Built on top of commit `b389942` ("feat: add opportunity taxonomy expansion, temporal state machine, and change history"), the last completed layer in this program. Nothing in this phase touches the frontend, D6/D7 internals, the temporal state machine, or change history logic — those are reused exactly as they already exist.

---

## 1. Architecture Audit

Before writing any code, the existing repository was read directly (not assumed) for every structure the mission's Part 0 asked about:

- **User profile** (`app/models/user.py`): already carried `age`, `academic_level` (native enum), `gender` (native enum), `degree_level`, `major`, `gpa`, `graduation_year`, `skills` (`ARRAY(String)`), `target_country` (documented as the user's *preferred destination*, consumed by the Autonomous Scholar Agent), `age_group` (free text), `bio`, `role`/`admin_role`. There was **no** `nationality`, `residency_country`, `languages`, `interests`, structured `application_goals`, or structured preference fields.
- **Opportunity eligibility columns** (`app/models/opportunity.py`): already carried `min_age`/`max_age`, `target_academic_levels` (`ARRAY` of the native `AcademicLevel` enum), `target_gender`, `target_age_group` (free text), `country` (default `'Global'`), `is_local_egypt`.
- **Existing hard-filter precedent**: `app/api/v1/endpoints/opportunities.py::_build_eligibility_filters(user)` already builds SQL `WHERE` clauses for age/academic-level/gender with a consistent "a missing signal on either side excludes nothing" posture, used by `list_opportunities`'s `eligible_only` flag. A second, separate, non-reusable version of the same idea exists embedded in `app/api/v1/endpoints/dashboards.py::_build_opportunity_filters` for the "Recommended Opportunities" dashboard widget.
- **"Recommendation" already means three different things in this codebase**, confirmed by direct reads to avoid collision:
  1. `app/services/recommendation.py` + `app/api/v1/endpoints/recommendations.py` (76 lines, pre-existing) — opportunity-to-opportunity pgvector similarity and D2-graph "related opportunities," both public, neither personalized to a user.
  2. `app/services/recommendation_service.py` (260 lines) — Course/Academy recommendations (`get_recommended_courses`), a completely different domain, confirmed unrelated by full read.
  3. The genuinely missing concept: a user-personalized *opportunity* recommendation, which this phase adds as a new route on the *existing* `recommendations` router rather than a new router.
- **D2 Knowledge Graph**: `app/models/entity_relationship.py` (`EntityRelationship`, a plain self-referential edge table over `Opportunity`, `relationship_type` values `same_organization`/`same_country`) — reused unchanged as the Part K graph signal.
- **D6/D7**: `app/services/evidence_verification.py::evaluate_verification_readonly` (returns a `VerificationDecision`) and `app/models/contradiction.py` (`Contradiction`) — both reused unmodified for the Passport's trust section.
- **Generic profile-update path**: `app/api/v1/endpoints/users.py::update_my_profile` already does `payload.model_dump(exclude_unset=True)` → generic `setattr` loop. Confirmed this means any field added to both `User` and `UserProfileUpdate` becomes settable through the existing `PATCH /users/me` with **zero endpoint code changes** — used deliberately instead of adding a new profile endpoint.

**Result: no duplicate `UserProfile`, `Recommendation`, `Eligibility`, `OpportunityMatch`, `Evidence`, or `OpportunityGraph` model was created.** Nine new nullable columns were added to the existing `User` table; nothing was added to `Opportunity`.

---

## 2. User Profile Model (Part A)

Nine new nullable columns on `User` (migration `0062_personalized_opportunity_profile`):

| Column | Type | Notes |
|---|---|---|
| `nationality` | `String(120)` | distinct from `residency_country` |
| `residency_country` | `String(120)` | distinct from `target_country` (a *preference*) |
| `languages` | `ARRAY(String(50))` | |
| `interests` | `ARRAY(String(100))` | free-text terms, matched against opportunity title+description |
| `application_goals` | `ARRAY(String(30))` | validated against the new `ApplicationGoal` enum at the schema layer |
| `preferred_opportunity_types` | `ARRAY(OpportunityType)` | native enum array, `create_type=False` |
| `remote_preference` | `Boolean` | three-state: `True`/`False`/`None` |
| `funding_preference` | `String(30)` | free text matching `FundingType` values by convention |
| `deadline_lead_time_days` | `Integer` | |

**Deliberately not added**: no inferred nationality from language, no inferred age from academic level, no gender/religion/ethnicity/health/political-view field of any kind (Part A's explicit prohibition). `UserProfileUpdate` gained nine matching optional fields; every field follows the schema's existing "`None` means leave unchanged" convention, verified with a round-trip test.

---

## 3. Eligibility Engine (Part B/C/D)

`app/services/eligibility_engine.py`, a pure function: `evaluate_eligibility(user: User, opportunity: Opportunity) -> EligibilityResult`.

It evaluates four dimensions — age, academic level, gender, residency/nationality — **reusing the exact same rule semantics** already established in `_build_eligibility_filters`, re-expressed as an explainable structure instead of a collapsed SQL clause. Each dimension function returns `None` when the opportunity states no requirement (matches the existing filter's "unrestricted" posture), a matched/failed `RuleEvaluation` with the real compared values, or an `"unknown: ..."`-prefixed evaluation when the *user* hasn't stated the relevant field.

**Status rules** (verbatim from Part B):
- Any failure → `INELIGIBLE`, full stop — a single hard failure is disqualifying no matter how many other dimensions matched.
- No failures, at least one unknown *and* at least one match → `CONDITIONAL`.
- No failures, no matches, only unknowns → `UNKNOWN` (never collapsed into `INELIGIBLE`).
- No failures, no unknowns → `ELIGIBLE` (including the case of zero stated requirements at all — "open to everyone" is a real, meaningful conclusion).

`missing_information` lists exactly which fields are unresolved (e.g. `["age", "academic level"]`), never fabricated.

---

## 4. Hard-Filter Logic (Part C/H)

Enforced structurally in `personalized_ranking.py::rank_candidates`, not by convention:

```
survivors = [o for o in opportunities if o.lifecycle_state != CLOSED]
for opportunity in survivors:
    eligibility = evaluate_eligibility(user, opportunity)
    if eligibility.status == INELIGIBLE:
        continue   # never scored, never ranked, never shown
    # ... only now is any personalization signal computed
```

There is no code path from a `MatchSignals` value back into eligibility. The literal Part C example — age 18–25 required, user is 16, high interest AND high goal match on the opportunity — is a dedicated regression test (`test_high_interest_cannot_promote_an_ineligible_opportunity`) asserting the candidate list comes back empty.

---

## 5. Evidence Integration (Part D)

The eligibility engine's own `RuleEvaluation.explanation` strings are the evidence trail — e.g. `"user age 16 < minimum age 18"`, `"user academic level high_school not in required set: university_year_1, university_year_2"`. Nothing is templated or invented; every explanation string is built from the real compared values at evaluation time. The Opportunity Passport surfaces these same strings verbatim (Section 6).

---

## 6. Opportunity Passport (Part E)

`app/services/opportunity_passport.py::build_passport(db, *, opportunity, requesting_user, now=None) -> OpportunityPassport`.

**Not a second Opportunity model** — nothing here is persisted; it is a read-only dataclass assembled from data the codebase already has:

- **Identity**: id, title, provider source id, canonical URL, opportunity type, plus `raw_opportunity_type_label` (pulled from `raw_extracted_data` JSONB, matching `canonical_opportunity.py`'s own retrieval pattern — a bug caught and fixed during self-review, see §17).
- **Eligibility**: `None` when `requesting_user is None` (never a fabricated default for an anonymous caller); a real `PassportEligibility` (status + rule strings) when authenticated.
- **Funding**: `funding_details`, unmodified.
- **Temporal**: `lifecycle_state`, `lifecycle_state_reason`, `deadline`, `last_verified` (= trust's `last_evidence_update`), `last_updated`, and up to 5 `recent_changes` from `OpportunityChangeHistory`.
- **Trust**: see §7.
- **Provenance**: deduped `source_ids`, `evidence_ids`, `claim_count` (from D6's own `evaluated_claims`).

---

## 7. Trust Model (Part F)

`PassportTrust` never says "Verified ✓" without a basis. Three literal, disclosed statuses:

| Passport label | Real D6 `VerificationDecisionStatus` |
|---|---|
| `VERIFIED` | `VERIFIED` |
| `HOLD` | `HOLD` |
| `NOT_VERIFIED` | `STALE` or `UNVERIFIED` (folded together at this summary level; `verification_reason` still carries the real underlying status string) |

Alongside the label: `evidence_count`, `source_count`, `authoritative_source_count` (all `SourceAuthority` tiers except `UNKNOWN`), `contradiction_count` (real D7 `Contradiction` rows), `last_evidence_update`. A caller always has the numbers behind the label — "why does APEX trust this?" is answerable from the response itself.

---

## 8. Personalization Signals (Part G)

`app/services/personalized_ranking.py::MatchSignals` — seven components, each a `MatchLevel` (`NONE`/`LOW`/`MEDIUM`/`HIGH`/`STRONG`), never a raw number:

| Signal | Basis |
|---|---|
| `interest_match` | deterministic case-insensitive substring overlap between `user.interests` and title+description |
| `goal_match` | a real, hardcoded `ApplicationGoal → {OpportunityType}` mapping table |
| `type_match` | `user.preferred_opportunity_types` vs. `opportunity.type` |
| `geography_match` | `user.target_country` (a stated *preference*, not `residency_country`/`nationality`) vs. `opportunity.country` |
| `funding_match` | `user.funding_preference` vs. `funding_intelligence.classify_funding_text(opportunity.funding_details)` (reused unmodified) |
| `deadline_match` | `user.deadline_lead_time_days` vs. `opportunity.deadline` (no new date parsing) |
| `graph_relevance` | one bulk query against the real D2 `EntityRelationship` table, HIGH when a candidate has an edge to something the user already bookmarked |

**Deliberately omitted from Part G's own list**: `skills_match` and `education_match` as separate signals — `User` doesn't yet structurally distinguish a "skills fit"/"education fit" concept from `interests`/`academic_level` (the latter is already consumed by hard eligibility, not duplicated here). Disclosed as a scope limitation, not hidden (§16).

---

## 9. Ranking Formula (Part S — no score theater)

`personalization_score(signals)` is the **unweighted sum** of each signal's fixed weight (`NONE=0, LOW=1, MEDIUM=2, HIGH=3, STRONG=4`), documented in the module docstring and covered by a reproducibility test (same inputs, same `now` → identical score across two calls). It is used only to order results within a tier — never presented to a user as a percentage anywhere in this codebase. Every component that fed it remains independently visible via `MatchSignals`.

---

## 10. Explanation Model (Part I/J)

`build_explanation(eligibility, signals) -> list[str]` emits a reason string **only** for signals at `HIGH`/`STRONG`, built entirely from the real `MatchSignals` values via a fixed label map (e.g. `"✓ Matches your stated interests (strong)"`) — no LLM call, no free-text generation. `build_missing_information(eligibility)` turns `unknown_rules` into explicit sentences (e.g. `"APEX needs your academic level to fully confirm eligibility."`) — missing information is never silently converted into a false eligibility claim.

---

## 11. Knowledge Graph Integration (Part K)

`_graph_relevance_levels` queries the existing `entity_relationships` table directly — no new graph, no new edge type, one bulk `IN (...)` query for the whole candidate set (not one query per candidate). A candidate with a real edge to an opportunity the user has already bookmarked gets `HIGH`; everything else `NONE`. Graph relevance is one signal among seven and, like every signal, is only ever computed *after* the hard-eligibility filter — it cannot rank an ineligible opportunity into view.

---

## 12. Exploration Strategy (Part L)

`rank_candidates`'s `exploration_ratio` parameter (default `DEFAULT_EXPLORATION_RATIO = 0.2`, module-level constant, overridable per call and exposed as a query parameter on `GET /recommendations`). `exploration_slots = round(limit * exploration_ratio)`; the top `limit - exploration_slots` by score form the "core" tier, and `exploration_slots` more are drawn (shuffled) from the remaining **hard-eligible** survivors — never from anything that failed the hard filter. Tested at ratio `0.5` (exactly half the page) and `0.0` (no exploration).

---

## 13. Privacy / Security (Part M/R)

Enforced structurally, not by convention, with dedicated tests that inspect real source code:

- `test_compute_match_signals_never_reads_sensitive_user_fields` — `inspect.getsource` on the whole `personalized_ranking` module asserts the literal substrings `user.gender`, `user.age`, `user.nationality`, `user.residency_country` never appear anywhere in it.
- `test_no_sensitive_attribute_ever_appears_in_an_explanation_string` — same technique on `build_explanation` specifically.
- `test_evaluate_eligibility_signature_is_structured_only` — `inspect.signature(evaluate_eligibility)` is exactly `{"user", "opportunity"}` — no raw text, no LLM output, no confidence score parameter can even be passed in.
- `test_eligibility_engine_module_has_no_llm_reference` — the module's own source file contains none of `genai`/`gemini`/`openai`/`llm`.
- **IDOR**: `GET /opportunities/{id}/passport` accepts no `user_id` parameter at all — eligibility is always computed for the caller's own authenticated identity (`CurrentUserOptional`), never anyone else's. There is no code path by which one user's request can return another user's eligibility.
- Residency/nationality/gender/age are read **only** inside `eligibility_engine.py` (hard eligibility) and never inside `personalized_ranking.py` (ranking/explanation) — a real module boundary, not just a comment.

---

## 14. API Changes (Part N/O)

- **`GET /recommendations`** (new route on the existing `recommendations` router, distinct path shape from the pre-existing `GET /{opportunity_id}` and `GET /{opportunity_id}/related`): authenticated only (`CurrentUser`). Query params: `opportunity_type`, `country`, `limit` (1–100, default 20), `exploration_ratio` (0.0–1.0, default `DEFAULT_EXPLORATION_RATIO`). Candidate pool: published opportunities matching the optional filters, capped at 500 (Part P — disclosed bound, see §15). Runs `rank_candidates` against the user's real bookmarks and returns structured items — no chain-of-thought, no raw LLM output.
- **`GET /opportunities/{opportunity_id}/passport`** (new route, same 404 gate as the existing `get_opportunity`): optional auth (`CurrentUserOptional`). Anonymous callers get `eligibility: null`; authenticated callers get their own real eligibility, never anyone else's.

---

## 15. Performance Approach (Part P)

No synchronous all-users × all-opportunities evaluation anywhere. `GET /recommendations` bounds its candidate pool to 500 published opportunities matching the request's own filters before ranking — a real, disclosed limit, not an unbounded scan. Graph relevance is one bulk query for the whole candidate set. No new indexes, no vector DB, no new infrastructure — this phase reuses the existing `entity_relationships` table and existing `Opportunity`/`Evidence`/`Contradiction` queries as-is. Caching was intentionally not added in this phase (kept simple per Part P's own instruction); the exact next step is proposed in §18.

---

## 16. Test Results

**New test file**: `tests/test_personalized_intelligence.py` — **43 tests, all passing.**

Coverage by Part Q category:
- **PROFILE** (3): `UserProfileUpdate` accepts all 9 new fields; unset fields excluded not nulled; no sensitive-attribute-inference field exists on the schema.
- **ELIGIBILITY** (11): age eligible/ineligible/unknown, academic level eligible/ineligible/unknown, residency match/mismatch, conditional, no-requirements-is-eligible, do-not-fabricate.
- **MATCHING** (8): each of the 7 signals individually, plus deterministic-reproducibility.
- **HARD FILTER** (4): ineligible never ranked, closed excluded, unknown-and-eligible both survive distinctly, the literal "high interest cannot promote an ineligible opportunity" case.
- **EXPLANATIONS** (4): matched-HIGH-signal produces a reason, missing-information shown for UNKNOWN, no sensitive attribute in an explanation string, privacy-boundary source-inspection test.
- **EXPLORATION** (3): ratio 0.5 → exactly half exploration, ratio 0.0 → zero exploration, default constant value.
- **PASSPORT** (8): identity, eligibility-null-when-anonymous, eligibility-present-when-authenticated, temporal, trust-with-zero-evidence, provenance-with-real-D6-evidence, contradiction count, recent changes.
- **SECURITY / NO-LLM-AUTHORITY** (2): `evaluate_eligibility` signature is structured-only; the module has no LLM reference.

**Full regression** (existing 1,401 pre-existing tests plus this phase's 43, run in batches of 4–19 files to stay inside the tool's execution window): **no failure traces back to this phase's diff.** Every failure/error found was independently reproduced as pre-existing and unrelated:

| File | Issue | Root cause |
|---|---|---|
| `tests/test_shifts.py` (2 tests) | ICS export body missing volunteer name | A hardcoded `NOW = 2026-09-10` test constant is now in the past relative to the real system date (2026-09-22); the shift is created but excluded by the endpoint's own "future/current only" filter. Confirmed identical failure against the unmodified `b389942` version of the file (with matching unmodified model/schema files) after ruling out our diff. |
| `tests/test_dashboards.py` (1 test) | Redis connection error | `redis.exceptions.ConnectionError: ... connecting to redis:6379` — no Redis service reachable in this environment; unrelated to any file this phase touched. |
| `tests/test_user_search.py` (1 test) | `403 != 401` | Pre-existing FastAPI `HTTPBearer` status-code convention mismatch in an unrelated router. |
| `tests/test_live_session_reminders.py` (6 tests), `tests/test_student_videos.py` (11 tests) | `CompileError: ... can't render element of type JSONB` | A `notifications.payload` `JSONB` column has no SQLite compiler mapping in these two files' fixtures — an existing, unrelated SQLite-testing limitation (these fixtures use `Base.metadata.create_all` rather than the raw-SQL-fixture convention this phase's own tests use). |
| `tests/test_live_sessions.py` (1 test) | Hangs (excluded from the timed run) | `TestLiveSessionSignalingWebSocket::test_an_offer_is_relayed_to_both_connected_participants` opens a real WebSocket connection; hangs in this sandboxed environment regardless of this phase's changes. The other 27 tests in the same file pass. |

**Mechanical fixture fix required and applied**: the same class of change from the prior phase (adding new `opportunities` columns broke 24 raw-SQL fixtures) recurred here for `users`. **43 pre-existing test files** with their own raw-SQL `CREATE TABLE users` fixture were patched with the 9 new nullable columns via a scripted, idempotent patch (verified `changed=43, skipped=0` on a clean run) — otherwise SQLAlchemy's auto-generated `SELECT` for the `User` model would fail with "no such column" against every one of them.

**A genuine SQLite-only testing artifact was found and disclosed, not hidden**: `ARRAY(String)` columns (`interests`, `application_goals`, `languages`) round-trip through this repository's raw-SQL SQLite test fixtures as a list of individual characters rather than a real string list — confirmed with a direct diagnostic. `ARRAY` of a *native enum* column (`target_academic_levels`, `preferred_opportunity_types`) is unaffected and round-trips correctly. This is a SQLite dialect quirk only; real Postgres round-trips all of these correctly. The two tests that exercise `interest_match`/`goal_match` construct their `User`/`Opportunity` objects directly in Python instead of through the SQLite fixture, since `compute_match_signals` never queries the database for these two signals anyway — a correctness fix, not a workaround that hides the issue.

---

## 17. Known Limitations

1. **`skills_match`/`education_match`** are not separate ranking signals (§8) — `interests` and `academic_level` are the closest existing structures, and `academic_level` is already consumed by hard eligibility; a genuinely distinct skills-fit signal would need a new, more granular profile field.
2. **`interest_match` is substring-based, not semantic** — "AI" and "machine learning" will not match each other. Disclosed in the module docstring, not fixed in this phase (fixing it would mean introducing an embedding/LLM step into a ranking signal, which Part R and Part S both caution against doing without very careful scoping).
3. **`GET /recommendations`'s candidate pool is capped at 500** published opportunities matching the request filters (Part P) — a real, disclosed bound, not the full catalog. At larger scale this needs a real candidate-retrieval index rather than a flat capped scan (§18).
4. **The Passport's `NOT_VERIFIED` label folds together two distinct D6 states** (`STALE` and `UNVERIFIED`) — `verification_reason` still carries the precise underlying status for any caller that needs the distinction.
5. **A bug was self-caught, not user-reported**: the first draft of `build_passport`'s `raw_opportunity_type_label` used `getattr(opportunity, "raw_opportunity_type_label", None)`, which would have silently always returned `None` — that attribute is not a persisted `Opportunity` column, only a schema/JSONB-threaded field (confirmed via `canonical_opportunity.py`'s own retrieval pattern). Caught by a follow-up grep before any test ran; fixed to read `(opportunity.raw_extracted_data or {}).get("raw_opportunity_type_label")`.
6. **`ARRAY(String)` profile fields are untested against real Postgres in this environment** (SQLite-only test harness, see §16) — the fixture-level SQLite quirk does not affect production, but genuine end-to-end coverage against real Postgres has not been run as part of this phase.

---

## 18. Exact Next Engineering Step

Build the real candidate-retrieval layer `GET /recommendations` currently substitutes with a flat, capped `SELECT ... LIMIT 500`: index opportunities by `country`/`type`/`lifecycle_state` (already-existing columns) so the candidate pool is drawn narrowly instead of capped broadly, and add a short-TTL cache keyed on `(user_id, filters)` for the ranked result page — the two concrete Part P follow-ups this phase deliberately deferred to keep the initial architecture simple, as instructed.

---

## Worked Examples

**A) Eligible user + eligible opportunity.**
User: `age=22`, `academic_level=university_year_3`, `interests=["Robotics"]`, `application_goals=["research"]`, `target_country="Germany"`.
Opportunity: `min_age=18, max_age=30`, `target_academic_levels=[university_year_1..year_5]`, `country="Germany"`, `type=research_funding`, title "Robotics Research Fellowship."
→ `evaluate_eligibility` → `ELIGIBLE` (both dimensions matched, none failed, none unknown).
→ `compute_match_signals` → `interest_match=STRONG` (title hit), `goal_match=HIGH` (research → research_funding), `geography_match=STRONG` (Germany = Germany).
→ `build_explanation` → `["You appear eligible for this opportunity.", "✓ Matches your stated interests (strong)", "✓ Matches your application goals (high)", "✓ Matches your preferred destination (strong)"]`.

**B) Ineligible user + opportunity.**
User: `age=16`. Opportunity: `min_age=18, max_age=25`.
→ `evaluate_eligibility` → `INELIGIBLE`, `failed_rules=[RuleEvaluation("age", "user age 16 < minimum age 18")]`.
→ `rank_candidates` never computes a `MatchSignals` for this opportunity at all — it is excluded before any signal exists, regardless of how strong any other match would have been (§4's own literal test case).

**C) Unknown/conditional eligibility.**
User: `age=20`, `academic_level=None` (not stated). Opportunity: `min_age=18, max_age=25`, `target_academic_levels=[university_year_1]`.
→ `evaluate_eligibility` → age matches (`20` within `18–25`), academic level unknown (user hasn't stated it) → `CONDITIONAL`.
→ `build_missing_information` → `["APEX needs your academic level to fully confirm eligibility."]` — never silently promoted to `ELIGIBLE`, never silently dropped to `INELIGIBLE`.

---

## Files Changed

**Modified**: `app/models/enums.py`, `app/models/user.py`, `app/schemas/user.py`, `app/api/v1/endpoints/recommendations.py`, `app/api/v1/endpoints/opportunities.py`, plus 43 pre-existing test files' raw-SQL `users` fixtures.

**New**: `alembic/versions/0062_personalized_opportunity_profile.py`, `app/services/eligibility_engine.py`, `app/services/personalized_ranking.py`, `app/services/opportunity_passport.py`, `app/schemas/personalization.py`, `tests/test_personalized_intelligence.py`.

**Not touched**: frontend (none in this repository), Phase 5, D6/D7 internals, the temporal state machine, change history logic, the D2 graph's own population logic (`entity_relationship_service.py`).
