"""Opportunity Monitoring & Personal Impact Engine, Parts A-F: the
composable DETECT -> VERIFY -> CLASSIFY -> RECORD stages of the
monitoring pipeline.

AUDITED FIRST (Part 0): `change_history_service.record_change_if_
meaningful` already guarantees "no change record for an identical
value" (Part B's own "do not treat every UPDATE as a meaningful
change"); it does NOT classify severity or gate on trust -- those are
this module's own, genuinely new responsibilities, added on top of that
existing function rather than duplicating it.

REUSES, NEVER DUPLICATES:
- `change_history_service.record_change_if_meaningful` for the actual
  write (Part F) -- this module only decides WHETHER and with WHAT
  severity to call it, never writes `OpportunityChangeHistory` any other
  way.
- `deadline_intelligence.classify_deadline_text`/`DeadlineState` for
  deadline-aware severity nuance (Part E) -- no second date parser.
- `app.models.enums.VerificationDecisionStatus` (D6's own VERIFIED/HOLD
  vocabulary) for this module's VERIFY step (Part D), rather than
  inventing a parallel status enum for the same "accept or hold"
  concept.

SCOPE, DISCLOSED (Part D): this module does NOT re-run the full D6/D7
evidence-and-contradiction pipeline per monitored field change. D6/D7
are built around `Claim`/`Evidence` rows tied to a single opportunity's
FIRST verification pass (`app/workers/tasks.py::_verify_new_
opportunities`, gated on `is_verified is False`); re-running that same
per-claim evidence machinery for every subsequent field re-check on an
ALREADY-verified opportunity would mean inventing a second Claim
lifecycle this codebase's own D6 design was never asked to support, and
was judged out of scope for this phase (see the report's own "Known
Limitations" section). Instead, this module's VERIFY step (`
verify_change_candidate`) reuses the SAME two trust boundaries this
codebase already relies on elsewhere:

1. A "backend-compared, structurally trusted" diff (`source_trust=
   "structured"`) -- e.g. `aggregator_service.upsert_opportunity`
   comparing its own freshly-scraped `OpportunityCreate` fields against
   the stored row. Same trust level this codebase already extended to
   its pre-existing 4-field change-tracking loop; VERIFIED unconditionally.
2. An AI re-extraction candidate (`source_trust="ai_reextraction"`) --
   e.g. `opportunity_reverification.py`'s own `ai_gateway.verify_and_
   embed` re-call. VERIFIED only when `is_legitimate` and
   `confidence_score` clear `settings.ai_confidence_threshold` -- the
   EXACT SAME gate `app/workers/tasks.py` already uses as its own first
   trust checkpoint before a NEW opportunity's fields are even written.
   Below that gate -> HOLD (Part D/R: "do not write an unverified change
   as if it were authoritative"; the observed difference is logged, not
   recorded as opportunity history, and never propagated to guidance/
   notifications).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ChangeSeverity, VerificationDecisionStatus
from app.models.opportunity import Opportunity
from app.models.opportunity_change_history import OpportunityChangeHistory
from app.services.change_history_service import normalize_for_comparison, record_change_if_meaningful
from app.services.deadline_intelligence import DeadlineState, classify_deadline_text

# Part B's own 15-category list, reduced to the fields that actually
# exist as real `Opportunity` columns this codebase can compare a real
# old value against a real new one for (Part 20 discipline: no field is
# tracked here that this pipeline cannot honestly derive a candidate
# value for). `title`/`description` are deliberately excluded from this
# registry even though `upsert_opportunity` already writes them -- see
# that function's own pre-existing docstring on why title stays tracked
# there directly (a disclosed, unchanged pre-existing behavior) while
# `description` never was (free-text, no stable severity rule -- Part
# C's own "punctuation changed -> IGNORE" example is exactly this
# field).
MONITORED_FIELDS: tuple[str, ...] = (
    "deadline",
    "eligibility_criteria",
    "funding_details",
    "lifecycle_state",
    "country",
    "is_local_egypt",
    "target_age_group",
    "min_age",
    "max_age",
    "target_gender",
    "target_academic_levels",
    "target_url",
    "review_status",
)


@dataclasses.dataclass
class DetectedChange:
    field_name: str
    old_value: object
    new_value: object


def detect_meaningful_changes(existing: Opportunity, candidate: dict) -> list[DetectedChange]:
    """Part B: pure comparison, no DB access, no network call. `candidate`
    is a plain `{field_name: new_value}` dict -- deliberately NOT an
    `Opportunity`/`OpportunityCreate` instance, so callers can pass a
    partial candidate (an AI re-extraction may only produce a subset of
    `MONITORED_FIELDS`) without constructing a throwaway ORM object.
    Fields not present in `candidate` are simply not compared -- absence
    is never treated as "changed to null" (Part B/Q: a caller that
    doesn't have fresh data for a field must never look like it observed
    that field going away).

    Uses the SAME `_normalize_for_comparison` `change_history_service`
    itself already uses to decide "genuinely different" -- one
    normalization rule, not two that could silently disagree.
    """
    changes: list[DetectedChange] = []
    for field_name in MONITORED_FIELDS:
        if field_name not in candidate:
            continue
        old_value = getattr(existing, field_name, None)
        new_value = candidate[field_name]
        if normalize_for_comparison(old_value) != normalize_for_comparison(new_value):
            changes.append(DetectedChange(field_name=field_name, old_value=old_value, new_value=new_value))
    return changes


def _severity_for_deadline(old_value, new_value) -> ChangeSeverity:
    """Part C/E worked examples: a deadline appearing/disappearing, or a
    real multi-day shift, is at least MATERIAL; a rolling/ongoing <->
    rolling/ongoing "change" (both classify to the same `DeadlineState`)
    is IGNORE -- reuses `deadline_intelligence.classify_deadline_text`'s
    own taxonomy rather than a second ad-hoc date-shift heuristic."""
    if old_value is None and new_value is not None:
        return ChangeSeverity.HIGH
    if old_value is not None and new_value is None:
        return ChangeSeverity.HIGH

    old_state = classify_deadline_text(None, parsed_deadline=old_value)
    new_state = classify_deadline_text(None, parsed_deadline=new_value)
    if old_state == DeadlineState.CLOSED or new_state == DeadlineState.CLOSED:
        return ChangeSeverity.CRITICAL

    if isinstance(old_value, dt.datetime) and isinstance(new_value, dt.datetime):
        old_dt = old_value if old_value.tzinfo else old_value.replace(tzinfo=dt.timezone.utc)
        new_dt = new_value if new_value.tzinfo else new_value.replace(tzinfo=dt.timezone.utc)
        shift_days = abs((new_dt - old_dt).days)
        if shift_days == 0:
            return ChangeSeverity.IGNORE
        if shift_days <= 2:
            return ChangeSeverity.MINOR
        if shift_days <= 14:
            return ChangeSeverity.MATERIAL
        return ChangeSeverity.HIGH

    return ChangeSeverity.MATERIAL


# Part C: deterministic, documented, per-field severity table for every
# field NOT handled by the deadline-specific function above. Each entry
# is a plain function of (old_value, new_value) -> ChangeSeverity --
# small enough to read as a table, testable one field at a time.
def _severity_for_eligibility_or_funding(old_value, new_value) -> ChangeSeverity:
    # Free text -- Part C's own "punctuation changed -> IGNORE" example
    # applies directly. Going from "nothing on file" to real text (or the
    # reverse) is a real, user-relevant change (HIGH); any other edit to
    # already-real text is at least MATERIAL (this codebase cannot
    # semantically diff prose deterministically without an LLM, and Part
    # C forbids using one as the classification authority -- MATERIAL,
    # not MINOR, is the honest, conservative default for "the text really
    # did change" so a real requirement/funding edit is never silently
    # under-classified into IGNORE-adjacent territory).
    if (old_value is None) != (new_value is None):
        return ChangeSeverity.HIGH
    return ChangeSeverity.MATERIAL


def _severity_for_lifecycle_state(old_value, new_value) -> ChangeSeverity:
    # Part C's own literal "OPEN -> CLOSED -> HIGH" example. Any
    # lifecycle transition is at minimum MATERIAL (it already passed
    # `opportunity_state_machine`'s own transition-table validation
    # before ever reaching this function, so it is never a noisy or
    # invalid value by construction) -- closing/expiring specifically is
    # promoted to HIGH/CRITICAL since it directly affects whether a user
    # should still be pursuing this opportunity at all.
    closing_states = {"closed", "expired"}
    if isinstance(new_value, str) and new_value.lower() in closing_states:
        return ChangeSeverity.CRITICAL
    if isinstance(old_value, str) and old_value.lower() in closing_states and (
        not isinstance(new_value, str) or new_value.lower() not in closing_states
    ):
        # Reopened -- also CRITICAL: a user who previously saw this as
        # closed and moved on needs to know it's viable again just as
        # much as they needed to know it closed.
        return ChangeSeverity.CRITICAL
    return ChangeSeverity.MATERIAL


def _severity_for_geography_or_age(old_value, new_value) -> ChangeSeverity:
    # `country`/`is_local_egypt`/`target_age_group`/`min_age`/`max_age`/
    # `target_gender`/`target_academic_levels` -- eligibility-adjacent
    # structured fields. A value appearing/disappearing (the field
    # becoming more or less restrictive in an unknown direction) is HIGH
    # -- this directly affects whether a user is even eligible; any
    # other edit between two real values is MATERIAL.
    if (old_value is None) != (new_value is None):
        return ChangeSeverity.HIGH
    return ChangeSeverity.MATERIAL


def _severity_for_application_url(old_value, new_value) -> ChangeSeverity:
    # The canonical application link changing at all is HIGH -- a user
    # partway through an application, or about to start one, following a
    # stale link is a real, concrete failure mode this codebase should
    # never let go unflagged.
    return ChangeSeverity.HIGH


def _severity_for_review_status(old_value, new_value) -> ChangeSeverity:
    # A published opportunity being un-published (or the reverse) is
    # CRITICAL -- the same "should the user still be pursuing this at
    # all" stakes as a lifecycle closure.
    return ChangeSeverity.CRITICAL


_SEVERITY_RULES = {
    "deadline": _severity_for_deadline,
    "eligibility_criteria": _severity_for_eligibility_or_funding,
    "funding_details": _severity_for_eligibility_or_funding,
    "lifecycle_state": _severity_for_lifecycle_state,
    "country": _severity_for_geography_or_age,
    "is_local_egypt": _severity_for_geography_or_age,
    "target_age_group": _severity_for_geography_or_age,
    "min_age": _severity_for_geography_or_age,
    "max_age": _severity_for_geography_or_age,
    "target_gender": _severity_for_geography_or_age,
    "target_academic_levels": _severity_for_geography_or_age,
    "target_url": _severity_for_application_url,
    "review_status": _severity_for_review_status,
}


def classify_change_severity(field_name: str, old_value, new_value) -> ChangeSeverity:
    """Part C: the one deterministic entry point every caller uses.
    Unknown field names (not in `MONITORED_FIELDS`) default to MATERIAL
    rather than raising -- a conservative, disclosed default (never
    silently IGNORE something this table simply hasn't been taught
    about yet)."""
    rule = _SEVERITY_RULES.get(field_name)
    if rule is None:
        return ChangeSeverity.MATERIAL
    return rule(old_value, new_value)


def verify_change_candidate(
    *, source_trust: str, ai_is_legitimate: bool | None = None, ai_confidence_score: float | None = None,
    ai_confidence_threshold: float | None = None,
) -> VerificationDecisionStatus:
    """Part D: the VERIFY step. `source_trust` is one of:

    - `"structured"`: the candidate came from backend code that already
      compared two real, structured values (e.g. `upsert_opportunity`'s
      own re-scrape) -- always VERIFIED, matching this codebase's own
      pre-existing trust level for that write path.
    - `"ai_reextraction"`: the candidate came from a fresh AI
      verification call -- VERIFIED only when `ai_is_legitimate` is
      `True` AND `ai_confidence_score` clears `ai_confidence_threshold`
      (defaults to `settings.ai_confidence_threshold` when omitted,
      imported lazily to avoid a hard settings dependency for callers
      that already pass an explicit threshold in tests); otherwise HOLD.
    """
    if source_trust == "structured":
        return VerificationDecisionStatus.VERIFIED

    if source_trust == "ai_reextraction":
        if ai_confidence_threshold is None:
            from app.core.config import settings

            ai_confidence_threshold = settings.ai_confidence_threshold
        if ai_is_legitimate and ai_confidence_score is not None and ai_confidence_score > ai_confidence_threshold:
            return VerificationDecisionStatus.VERIFIED
        return VerificationDecisionStatus.HOLD

    raise ValueError(f"unknown source_trust {source_trust!r}")


@dataclasses.dataclass
class RecordedChange:
    history_row: OpportunityChangeHistory
    severity: ChangeSeverity


async def record_verified_changes(
    db: AsyncSession,
    *,
    opportunity_id: uuid.UUID,
    changes: list[DetectedChange],
    verification_status: VerificationDecisionStatus,
    change_reason: str,
    source_id: uuid.UUID | None = None,
    evidence_id: uuid.UUID | None = None,
) -> list[RecordedChange]:
    """Part C/D/F combined: classify each detected change, skip both
    HOLD-verification candidates (Part D -- never write an unverified
    change as opportunity history) and IGNORE-severity ones (Part B/C --
    never create noise for a change nobody should see), and record
    everything else via the existing, real `record_change_if_meaningful`
    (which itself still re-checks old != new -- belt and suspenders, but
    never a second source of truth: THIS function's own `changes` list
    is what decides whether anything is even attempted).

    Returns only the changes that were actually recorded -- callers
    (Part G's personal-impact stage) act on this return value, never on
    `changes` directly, so a HOLD or IGNORE change can never reach a
    user."""
    if verification_status != VerificationDecisionStatus.VERIFIED:
        return []

    recorded: list[RecordedChange] = []
    for change in changes:
        severity = classify_change_severity(change.field_name, change.old_value, change.new_value)
        if severity == ChangeSeverity.IGNORE:
            continue

        row = await record_change_if_meaningful(
            db,
            opportunity_id=opportunity_id,
            field_name=change.field_name,
            old_value=change.old_value,
            new_value=change.new_value,
            change_reason=change_reason,
            source_id=source_id,
            evidence_id=evidence_id,
        )
        if row is None:
            continue
        row.severity = severity.value
        db.add(row)
        # Explicit flush (not just `db.add`) -- `record_change_if_
        # meaningful` already flushed its own INSERT, but this severity
        # assignment happens AFTER that flush, so without a second flush
        # here the row stays dirty in the session across this whole
        # function's return. A caller with nothing left to commit (e.g.
        # a MATERIAL-severity change that never reaches the notification
        # step's own commit) would otherwise leave that dirty UPDATE
        # pending until the next unrelated query anywhere else in the
        # same session triggers autoflush -- which, for a plain
        # synchronous relationship attribute access outside an `await`
        # (e.g. `user.bookmarks` in `user_intelligence.py`), raises
        # `MissingGreenlet` (real bug, found and fixed while writing this
        # phase's own test suite -- see the report's own Known
        # Limitations/fixes section).
        await db.flush()
        recorded.append(RecordedChange(history_row=row, severity=severity))

    return recorded
