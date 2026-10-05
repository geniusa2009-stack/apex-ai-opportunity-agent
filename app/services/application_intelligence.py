"""Application Intelligence step, Part J: the read-oriented
`ApplicationIntelligenceSnapshot` projection for one tracked application.

NOT A SECOND APPLICATION MODEL (Part J's own explicit rule, same posture
as `user_intelligence.py`'s own `UserIntelligenceSnapshot`): nothing
here is persisted beyond what `application_tracker_service` already
writes. This module reads an already-owned `ApplicationTracker`
(ownership is the CALLER's responsibility -- see
`application_tracker_service.get_owned_tracker`), its linked
`Opportunity` (if any), `ApplicationRequirementCompletion` rows, and
`OpportunityChangeHistory` rows, and assembles a plain dataclass,
computed fresh on every call.

REUSES, NEVER DUPLICATES:
- `application_requirements.extract_requirements` for the deterministic
  requirement list (Part C).
- `deadline_intelligence.classify_deadline_text` for the deadline state
  taxonomy (Part G) -- no second deadline parser.
- `guidance_service`'s application-guidance generators (Part F) for
  `next_actions`/`guidance` -- no second, competing guidance engine.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_requirement_completion import ApplicationRequirementCompletion
from app.models.application_tracker import ApplicationTracker
from app.models.opportunity import Opportunity
from app.models.opportunity_change_history import OpportunityChangeHistory
from app.services import application_requirements
from app.services.deadline_intelligence import DeadlineState, classify_deadline_text

# Application Intelligence step, Part I: how far back a "recent
# opportunity change" is still worth surfacing in one application's own
# snapshot -- same window as `guidance_service.RECENT_CHANGE_WINDOW_DAYS`,
# deliberately kept identical rather than a second, different constant.
RECENT_CHANGE_WINDOW_DAYS = 7


@dataclasses.dataclass
class RequirementStatusItem:
    key: str
    label: str
    status: str  # required | conditional | optional (from application_requirements)
    source_field: str
    evidence_snippet: str
    completion: str  # "complete" | "missing" -- USER_CONFIRMED_COMPLETE only, never inferred
    completed_at: dt.datetime | None


@dataclasses.dataclass
class ChecklistTask:
    task_id: int
    task_name: str
    is_completed: bool


@dataclasses.dataclass
class OpportunityChangeItem:
    field_name: str
    old_value: str | None
    new_value: str | None
    changed_at: dt.datetime


@dataclasses.dataclass
class ApplicationIntelligenceSnapshot:
    tracker_id: uuid.UUID
    status: str
    scholarship_name: str
    institution: str | None
    opportunity_id: uuid.UUID | None
    opportunity_title: str | None
    deadline: dt.datetime | None
    deadline_state: str  # DeadlineState value
    requirements: list[RequirementStatusItem]
    ai_checklist: list[ChecklistTask]
    recent_opportunity_changes: list[OpportunityChangeItem]
    is_submitted: bool
    submitted_at: dt.datetime | None
    generated_at: dt.datetime


async def _load_requirements(
    db: AsyncSession, *, tracker: ApplicationTracker, opportunity: Opportunity | None
) -> list[RequirementStatusItem]:
    if opportunity is None:
        # Part C's own rule, enforced structurally: no catalog Opportunity
        # -> no verified text to derive requirements from -> an honestly
        # empty list, never a generic fallback ("you'll probably need a
        # CV") invented for an off-catalog tracker.
        return []

    raw_requirements = application_requirements.extract_requirements(opportunity)
    completions = await _get_completions(db, tracker.id)

    items: list[RequirementStatusItem] = []
    for req in raw_requirements:
        completion_row = completions.get(req.key)
        is_complete = completion_row is not None and completion_row.is_completed
        items.append(
            RequirementStatusItem(
                key=req.key, label=req.label, status=req.status,
                source_field=req.source_field, evidence_snippet=req.evidence_snippet,
                completion="complete" if is_complete else "missing",
                completed_at=completion_row.completed_at if completion_row else None,
            )
        )
    return items


async def _get_completions(db: AsyncSession, tracker_id: uuid.UUID) -> dict[str, ApplicationRequirementCompletion]:
    result = await db.execute(
        select(ApplicationRequirementCompletion).where(
            ApplicationRequirementCompletion.tracker_id == tracker_id,
        )
    )
    return {row.requirement_key: row for row in result.scalars().all()}


async def _recent_opportunity_changes(
    db: AsyncSession, *, opportunity_id: uuid.UUID | None, now: dt.datetime
) -> list[OpportunityChangeItem]:
    if opportunity_id is None:
        return []
    cutoff = now - dt.timedelta(days=RECENT_CHANGE_WINDOW_DAYS)
    result = await db.execute(
        select(OpportunityChangeHistory).where(
            OpportunityChangeHistory.opportunity_id == opportunity_id,
            OpportunityChangeHistory.changed_at >= cutoff,
        ).order_by(OpportunityChangeHistory.changed_at.desc())
    )
    return [
        OpportunityChangeItem(
            field_name=c.field_name, old_value=c.old_value, new_value=c.new_value, changed_at=c.changed_at,
        )
        for c in result.scalars().all()
    ]


def _deadline_state(opportunity: Opportunity | None, deadline: dt.datetime | None) -> DeadlineState:
    if opportunity is None:
        # Off-catalog tracker: the only real text available is whatever
        # the user themselves typed as `notes`/`scholarship_name` -- not
        # verified opportunity evidence, so classifying against it would
        # misrepresent an unverified guess as a evidence-based
        # classification. `classify_deadline_text` on a real stored
        # `deadline` alone still gives an honest EXACT/DATE_ONLY/UNKNOWN
        # answer; with no deadline at all, UNKNOWN is correct.
        return classify_deadline_text(None, parsed_deadline=deadline)
    raw_text = " ".join(
        filter(None, [opportunity.eligibility_criteria, opportunity.description, opportunity.funding_details])
    )
    return classify_deadline_text(raw_text or None, parsed_deadline=deadline)


def _current_deadline(opportunity: Opportunity | None, tracker: ApplicationTracker) -> dt.datetime | None:
    """P0-C (Product-Wide Integration Recovery): the single place that
    decides which deadline value is CURRENT for this snapshot.

    `Opportunity.deadline` is the canonical, live value -- kept fresh by
    `opportunity_reverification.py`/`app/workers/tasks.py` every time the
    opportunity is re-verified. `ApplicationTracker.deadline` is only a
    ONE-TIME SNAPSHOT taken by `application_tracker_service.create_tracker`
    at tracker-creation time and never resynced afterward (confirmed via
    direct repository inspection, not just the audit doc, before this fix
    was written) -- reading it as if it were "the current deadline" is
    exactly the stale self-contradiction this phase's audit found
    (F-STALEDEADLINE).

    Fix: prefer the live `opportunity.deadline` whenever a real,
    catalog-linked `Opportunity` is available -- even if that value is
    `None` (e.g. the opportunity has since gone rolling/undated; showing
    an honestly-unknown deadline beats resurrecting a stale snapshot).
    `tracker.deadline` is used ONLY as the fallback for the genuinely
    off-catalog case (`opportunity is None`), where no live value exists
    at all. This never mutates `tracker.deadline` itself -- it stays
    exactly what `create_tracker` set, untouched, for every other
    consumer (e.g. `deadline_alerts.py`'s milestone planning, which is
    deliberately a separate, frozen-at-generation-time concept reviewed
    and excluded from this fix).
    """
    if opportunity is not None:
        return opportunity.deadline
    return tracker.deadline


async def build_application_intelligence_snapshot(
    db: AsyncSession, *, tracker: ApplicationTracker, now: dt.datetime | None = None
) -> ApplicationIntelligenceSnapshot:
    now = now or dt.datetime.now(dt.timezone.utc)

    opportunity = tracker.opportunity  # lazy="selectin" relationship, already loaded

    current_deadline = _current_deadline(opportunity, tracker)
    deadline_state = _deadline_state(opportunity, current_deadline)
    requirements = await _load_requirements(db, tracker=tracker, opportunity=opportunity)
    changes = await _recent_opportunity_changes(db, opportunity_id=tracker.opportunity_id, now=now)

    ai_checklist = [
        ChecklistTask(task_id=t["task_id"], task_name=t["task_name"], is_completed=t["is_completed"])
        for t in tracker.ai_todo_list
    ]

    return ApplicationIntelligenceSnapshot(
        tracker_id=tracker.id,
        status=tracker.status,
        scholarship_name=tracker.scholarship_name,
        institution=tracker.institution,
        opportunity_id=tracker.opportunity_id,
        opportunity_title=opportunity.title if opportunity else None,
        deadline=current_deadline,
        deadline_state=deadline_state.value,
        requirements=requirements,
        ai_checklist=ai_checklist,
        recent_opportunity_changes=changes,
        is_submitted=tracker.submitted_at is not None,
        submitted_at=tracker.submitted_at,
        generated_at=now,
    )
