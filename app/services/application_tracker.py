import logging
from datetime import datetime, timezone
from uuid import UUID

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.models.application_requirement_completion import ApplicationRequirementCompletion
from app.models.application_tracker import ApplicationTracker
from app.models.enums import OpportunityReviewStatus, TrackerStatus
from app.models.opportunity import Opportunity
from app.services import application_state_machine

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TrackerNotFoundError(Exception):
    """Raised when a `tracker_id` doesn't exist, or exists but isn't
    owned by the given `user_id` -- same "don't distinguish the two
    cases" posture as every other user-scoped lookup in this codebase
    (e.g. app/api/v1/endpoints/history.py's `_get_owned_history_or_404`)."""


class TodoTaskNotFoundError(Exception):
    """Raised when a `task_id` doesn't exist within a tracker's
    `ai_todo_list`."""


class OpportunityNotFoundError(Exception):
    """Raised when a `create_tracker` call names an `opportunity_id`
    that doesn't exist, or isn't published (Application Intelligence
    step, Part A) -- a tracker must never silently link to nothing."""


class DuplicateApplicationError(Exception):
    """Raised when the caller already has a tracker linked to this same
    `opportunity_id` (Application Intelligence step, Part T)."""


class TodoGenerationError(Exception):
    """Raised when the Gemini call that generates the initial
    preparation checklist fails."""


class _GeneratedTodoList(BaseModel):
    """Structured output for the checklist-generation call. Gemini only
    ever produces the task names -- `task_id` (sequential) and
    `is_completed` (always False initially) are added by `create_tracker`
    below before this is persisted, matching `ApplicationTracker.ai_todo_list`'s
    stored shape (see that column's comment)."""

    tasks: list[str] = Field(
        min_length=4,
        max_length=6,
        description="4 to 6 highly realistic, specific, step-by-step "
        "preparation tasks tailored to this exact scholarship -- not "
        "generic advice. E.g. for a scholarship known for valuing "
        "leadership, a task like 'Draft the leadership/networking essay, "
        "grounded in 2-3 concrete examples' rather than a generic 'Write "
        "your essay'. Ordered roughly in the sequence a student should "
        "tackle them.",
    )


_TODO_SYSTEM_PROMPT = """\
You are a scholarship application strategist helping a student prepare a \
step-by-step preparation checklist for one specific scholarship they intend to \
apply to.

You will be given the scholarship's name and, if known, its institution.

Your job: generate 4 to 6 concrete, actionable preparation tasks, ordered \
roughly in the sequence a student should tackle them.

Rules:
- Tailor the tasks specifically to what this particular scholarship is \
genuinely known for requiring or valuing (e.g. a leadership-focused \
scholarship should get a task about drafting a leadership essay grounded in \
concrete examples; a research-focused scholarship should get a task about \
drafting a research proposal) -- if you genuinely recognize this scholarship, \
be specific about it. If you don't recognize it, or aren't confident about its \
actual specific requirements, generate realistic, generally-applicable \
scholarship application tasks instead (gathering transcripts and \
recommendation letters, drafting a personal statement, preparing financial \
documents, etc.) rather than inventing specific requirements, portal names, or \
document titles that may not actually be real for this scholarship.
- Each task must be a single, concrete, actionable step -- not a vague \
category like "Prepare documents".
- Do not make "research the scholarship" your only or primary task -- go \
deeper into what actually preparing a strong application requires.
"""


def _client() -> genai.Client:
    # Same "fresh client per call" posture as every other Gemini-backed
    # service in this codebase -- see VisionGateway._client
    # (app/services/vision.py) for the full event-loop-lifetime reasoning.
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def _generate_todo_tasks(scholarship_name: str, institution: str | None) -> list[str]:
    prompt = f"Scholarship: {scholarship_name}"
    if institution:
        prompt += f"\nInstitution: {institution}"

    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_tracker_model,
            contents=[_TODO_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_GeneratedTodoList,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise TodoGenerationError(str(exc)) from exc

    if not response.text:
        raise TodoGenerationError("Gemini returned no content for to-do list generation")

    return _GeneratedTodoList.model_validate_json(response.text).tasks


async def create_tracker(
    db: AsyncSession,
    user_id: UUID,
    scholarship_name: str | None = None,
    institution: str | None = None,
    deadline: datetime | None = None,
    opportunity_id: UUID | None = None,
) -> ApplicationTracker:
    """Generates a scholarship-tailored preparation checklist via Gemini,
    then creates the tracker row with it.

    Takes `db: AsyncSession` as an explicit first parameter -- not in the
    ask's literal signature (`create_tracker(user_id, scholarship_name,
    institution, deadline=None)`), but the same necessary-plumbing
    addition already established and disclosed for
    `memory_service.generate_and_store_embedding` and
    `opportunity_search.semantic_search`: this function writes a row and
    needs a session to do it with, and services that touch the DB in this
    codebase accept an injected session rather than opening their own.

    Application Intelligence step, Part A/K addition: `opportunity_id`
    (optional, preserves every pre-existing off-catalog call site
    unmodified). When given, the linked `Opportunity` must exist and be
    published (`OpportunityNotFoundError` otherwise -- same 404 posture
    as every other opportunity-facing endpoint), `scholarship_name`
    defaults to the opportunity's own real `title` when the caller didn't
    supply one, and `deadline` defaults to the opportunity's own real,
    verified `deadline` when the caller didn't supply one -- never
    invented, always the catalog's own stored value. Part T's duplicate
    protection is checked here (a fast, real pre-insert check) in
    addition to the database's own partial unique index, which is the
    actual race-safe backstop (see the `IntegrityError` handling below).
    """
    opportunity: Opportunity | None = None
    if opportunity_id is not None:
        opportunity = await db.get(Opportunity, opportunity_id)
        if opportunity is None or opportunity.review_status != OpportunityReviewStatus.PUBLISHED.value:
            raise OpportunityNotFoundError(str(opportunity_id))

        existing = await db.execute(
            select(ApplicationTracker).where(
                ApplicationTracker.user_id == user_id,
                ApplicationTracker.opportunity_id == opportunity_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise DuplicateApplicationError(str(opportunity_id))

        if scholarship_name is None:
            scholarship_name = opportunity.title
        if deadline is None:
            deadline = opportunity.deadline

    if scholarship_name is None:
        raise ValueError("scholarship_name is required when opportunity_id is not provided")

    tasks = await _generate_todo_tasks(scholarship_name, institution)

    ai_todo_list = [
        {"task_id": i, "task_name": task_name, "is_completed": False}
        for i, task_name in enumerate(tasks, start=1)
    ]

    tracker = ApplicationTracker(
        user_id=user_id,
        scholarship_name=scholarship_name,
        institution=institution,
        status=TrackerStatus.INTERESTED.value,
        deadline=deadline,
        opportunity_id=opportunity_id,
        ai_todo_list=ai_todo_list,
    )
    db.add(tracker)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Race-safe backstop for Part T -- two concurrent requests both
        # pass the pre-insert check above, then only one commit wins
        # against `uq_application_trackers_user_opportunity`. Same
        # "roll back and treat it as the real duplicate" posture as
        # `bookmarks.py::create_bookmark`.
        await db.rollback()
        raise DuplicateApplicationError(str(opportunity_id)) from exc
    await db.refresh(tracker)
    return tracker


async def get_user_trackers(db: AsyncSession, user_id: UUID) -> list[ApplicationTracker]:
    """Returns every tracker for `user_id`, newest first. Unpaginated --
    per the ask's literal "Returns all trackers for the active user"
    (distinct from how the History step's ask asked for open-ended
    "standard CRUD", where a paginated list was this project's own
    scoping choice); a personal Kanban board is bounded in a way a
    catalog listing isn't, so an unpaginated list matches both the
    literal ask and the actual use case."""
    result = await db.execute(
        select(ApplicationTracker)
        .where(ApplicationTracker.user_id == user_id)
        .order_by(ApplicationTracker.created_at.desc())
    )
    return list(result.scalars().all())


async def _get_owned_tracker(
    db: AsyncSession, user_id: UUID, tracker_id: UUID
) -> ApplicationTracker:
    result = await db.execute(
        select(ApplicationTracker).where(
            ApplicationTracker.id == tracker_id, ApplicationTracker.user_id == user_id
        )
    )
    tracker = result.scalar_one_or_none()
    if tracker is None:
        raise TrackerNotFoundError(str(tracker_id))
    return tracker


async def get_owned_tracker(db: AsyncSession, user_id: UUID, tracker_id: UUID) -> ApplicationTracker:
    """Public alias for `_get_owned_tracker` -- Application Intelligence
    step's own read-only services (`application_intelligence.py`,
    `guidance_service.py`) need the same IDOR-safe ownership lookup this
    module already established; exposing it under a non-underscored name
    rather than duplicating the query."""
    return await _get_owned_tracker(db, user_id, tracker_id)


async def update_tracker_status(
    db: AsyncSession, user_id: UUID, tracker_id: UUID, new_status: str
) -> ApplicationTracker:
    """Application Intelligence step, Part B/U addition: every transition
    now runs through `application_state_machine.validate_generic_transition`
    before being applied -- an invalid transition (including any attempt
    to reach APPLIED through this generic path; see that module's own
    docstring) raises `InvalidTransitionError` and leaves the tracker
    unchanged."""
    tracker = await _get_owned_tracker(db, user_id, tracker_id)
    application_state_machine.validate_generic_transition(tracker.status, new_status)
    tracker.status = new_status
    db.add(tracker)
    await db.commit()
    await db.refresh(tracker)
    return tracker


async def submit_tracker(db: AsyncSession, user_id: UUID, tracker_id: UUID) -> ApplicationTracker:
    """Application Intelligence step, Part K/M: the ONE controlled
    pathway that can move a tracker to APPLIED. Stamps `submitted_at`
    (Part M's own explicit submission record) in the SAME transaction as
    the status change -- both or neither, never a status of "applied"
    with no real submission timestamp behind it.

    Submitting does NOT integrate with any real external application
    system (none exists in this codebase) -- this call means exactly
    "the user explicitly told APEX they submitted this application",
    never "APEX itself submitted anything on the user's behalf" (Part M's
    own literal distinction)."""
    tracker = await _get_owned_tracker(db, user_id, tracker_id)
    application_state_machine.validate_submission_transition(tracker.status)
    tracker.status = TrackerStatus.APPLIED.value
    tracker.submitted_at = _utcnow()
    db.add(tracker)
    await db.commit()
    await db.refresh(tracker)
    return tracker


async def complete_requirement(
    db: AsyncSession, user_id: UUID, tracker_id: UUID, requirement_key: str
) -> ApplicationRequirementCompletion:
    """Application Intelligence step, Part D/E: explicit
    USER_CONFIRMED_COMPLETE for one deterministic, opportunity-derived
    requirement. Upserts -- calling this twice for the same requirement
    is a no-op, not an error (same posture as `bookmarks.py::
    create_bookmark`)."""
    await _get_owned_tracker(db, user_id, tracker_id)  # IDOR check, raises TrackerNotFoundError

    result = await db.execute(
        select(ApplicationRequirementCompletion).where(
            ApplicationRequirementCompletion.tracker_id == tracker_id,
            ApplicationRequirementCompletion.requirement_key == requirement_key,
        )
    )
    row = result.scalar_one_or_none()
    now = _utcnow()
    if row is None:
        row = ApplicationRequirementCompletion(
            tracker_id=tracker_id, requirement_key=requirement_key,
            is_completed=True, completed_at=now,
        )
        db.add(row)
    elif not row.is_completed:
        row.is_completed = True
        row.completed_at = now

    await db.commit()
    await db.refresh(row)
    return row


async def uncomplete_requirement(
    db: AsyncSession, user_id: UUID, tracker_id: UUID, requirement_key: str
) -> None:
    """Reverses `complete_requirement` -- deletes the completion row
    entirely (rather than flipping a flag) so a re-derived requirements
    projection and this table never disagree about whether a completion
    record exists at all."""
    await _get_owned_tracker(db, user_id, tracker_id)  # IDOR check

    result = await db.execute(
        select(ApplicationRequirementCompletion).where(
            ApplicationRequirementCompletion.tracker_id == tracker_id,
            ApplicationRequirementCompletion.requirement_key == requirement_key,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()


async def get_requirement_completions(
    db: AsyncSession, tracker_id: UUID
) -> dict[str, ApplicationRequirementCompletion]:
    """Every completion row for one tracker, keyed by `requirement_key` --
    used by `application_intelligence.py` to annotate a freshly-derived
    requirements projection with real completion state."""
    result = await db.execute(
        select(ApplicationRequirementCompletion).where(
            ApplicationRequirementCompletion.tracker_id == tracker_id,
        )
    )
    return {row.requirement_key: row for row in result.scalars().all()}


async def toggle_todo_task(
    db: AsyncSession, user_id: UUID, tracker_id: UUID, task_id: int, is_completed: bool
) -> ApplicationTracker:
    tracker = await _get_owned_tracker(db, user_id, tracker_id)

    updated_list = []
    found = False
    for task in tracker.ai_todo_list:
        if task["task_id"] == task_id:
            task = {**task, "is_completed": is_completed}
            found = True
        updated_list.append(task)

    if not found:
        raise TodoTaskNotFoundError(f"task_id {task_id} not found in tracker {tracker_id}")

    # Whole-attribute reassignment, not an in-place `.append()`/item
    # mutation -- SQLAlchemy doesn't track in-place mutations of a plain
    # JSONB-mapped Python list/dict by default (that needs
    # `sqlalchemy.ext.mutable`, not introduced here to keep this simple).
    # Reassigning the whole attribute is what actually marks the column
    # dirty so the UPDATE is emitted.
    tracker.ai_todo_list = updated_list

    db.add(tracker)
    await db.commit()
    await db.refresh(tracker)
    return tracker


async def delete_tracker(db: AsyncSession, user_id: UUID, tracker_id: UUID) -> None:
    tracker = await _get_owned_tracker(db, user_id, tracker_id)
    await db.delete(tracker)
    await db.commit()
