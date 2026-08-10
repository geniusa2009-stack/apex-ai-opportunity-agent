import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.models.deadline_alert import DeadlineAlert

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class MilestoneGenerationError(Exception):
    """Raised when the Gemini call that generates the milestone plan fails."""


class AlertNotFoundError(Exception):
    """Raised when an `alert_id` doesn't exist, or exists but isn't owned
    by the given `user_id` -- same "don't distinguish the two cases"
    posture as every other user-scoped lookup in this codebase."""


class _GeneratedMilestone(BaseModel):
    # Gemini is asked for a RELATIVE offset (days before the deadline),
    # not an absolute date -- deliberately: LLM date arithmetic against a
    # given "today" is a known source of off-by-one/timezone errors, and
    # this project's own code already has the real `deadline` and can do
    # exact arithmetic itself (see `generate_milestones` below, which
    # also clips this value defensively rather than trusting it blindly).
    days_before_deadline: int = Field(
        ge=0,
        description="How many whole days before the deadline this "
        "milestone should fire, e.g. 30 for 'a month before the "
        "deadline'. Must be less than or equal to the total days "
        "remaining given to you, and 0 or greater (0 means the deadline "
        "day itself).",
    )
    title: str = Field(
        description="Short, specific milestone title, e.g. 'Request "
        "recommendation letters for the Chevening Scholarship'."
    )
    description: str = Field(
        description="Concrete, actionable advice on exactly what the "
        "student should be doing at this point in their preparation."
    )


class _GeneratedMilestoneList(BaseModel):
    # "3 to 5 key milestone alerts" per the ask -- a genuine structural
    # requirement, hard-constrained the same way the Kanban Tracker step
    # constrained its 4-6 checklist items.
    milestones: list[_GeneratedMilestone] = Field(min_length=3, max_length=5)


_MILESTONE_SYSTEM_PROMPT = """\
You are a scholarship application strategist helping a student plan backward \
from an application deadline. Simply reminding a student on the final day is \
useless -- your job is to identify the key strategic checkpoints between now \
and the deadline.

You will be given the scholarship's name and how many whole days remain \
until its deadline.

Your job: generate 3 to 5 milestone alerts, each with a `days_before_deadline` \
offset (0 = the deadline itself), a short title, and concrete, actionable \
advice on what the student should be doing at that point.

Rules:
- Space the milestones out sensibly across however much time actually \
remains -- do not propose a milestone at "30 days before" if fewer than 30 \
days remain in total. Scale your milestone spacing to the actual time \
available; if very little time remains, propose closer, more urgent \
milestones instead.
- Order milestones from furthest from the deadline to closest (largest \
`days_before_deadline` first).
- Tailor the advice specifically to what this particular scholarship is \
genuinely known for requiring, if you recognize it (e.g. essay-heavy, \
interview-heavy, requires multiple recommendation letters). If you don't \
recognize it or aren't confident about its specific requirements, use \
realistic, generally-applicable scholarship preparation milestones instead \
(recommendation letters, personal statement drafts, financial documents, \
final proofreading/submission) rather than inventing specific requirements \
that may not be real for this scholarship.
- Each milestone's advice must be a concrete, actionable step -- not a vague \
category like "prepare documents".
"""


def _client() -> genai.Client:
    # Same "fresh client per call" posture as every other Gemini-backed
    # service in this codebase -- see VisionGateway._client
    # (app/services/vision.py) for the full event-loop-lifetime reasoning.
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def _generate_milestone_plan(
    scholarship_name: str, days_remaining: int
) -> list[_GeneratedMilestone]:
    prompt = (
        f"Scholarship: {scholarship_name}\n"
        f"Days remaining until the application deadline: {days_remaining}"
    )

    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_deadline_alerts_model,
            contents=[_MILESTONE_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_GeneratedMilestoneList,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise MilestoneGenerationError(str(exc)) from exc

    if not response.text:
        raise MilestoneGenerationError("Gemini returned no content for milestone generation")

    return _GeneratedMilestoneList.model_validate_json(response.text).milestones


async def generate_milestones(
    db: AsyncSession,
    user_id: UUID,
    tracker_id: UUID,
    scholarship_name: str,
    deadline: datetime,
) -> list[DeadlineAlert]:
    """Generates 3-5 strategic milestone alerts backward from `deadline`
    via Gemini, and persists them.

    Takes `db: AsyncSession` as an explicit first parameter -- not in the
    ask's literal signature, but the same necessary-plumbing addition
    already established and disclosed twice over in this project
    (`memory_service`, `application_tracker`'s `create_tracker`): this
    function writes rows and needs a session to do it with.
    """
    now = datetime.now(timezone.utc)
    deadline_utc = deadline if deadline.tzinfo is not None else deadline.replace(tzinfo=timezone.utc)
    days_remaining = max((deadline_utc - now).days, 0)

    raw_milestones = await _generate_milestone_plan(scholarship_name, days_remaining)

    alerts: list[DeadlineAlert] = []
    for milestone in raw_milestones:
        # Clip Gemini's offset into the valid [0, days_remaining] window
        # before doing the actual date arithmetic ourselves -- instructed
        # not to exceed `days_remaining` in the prompt, but never trusted
        # blindly; a milestone landing before "now" or after the deadline
        # itself would be a broken/pointless alert.
        days_before = min(max(milestone.days_before_deadline, 0), days_remaining)
        scheduled_date = deadline_utc - timedelta(days=days_before)
        if scheduled_date < now:
            scheduled_date = now

        alert = DeadlineAlert(
            user_id=user_id,
            tracker_id=tracker_id,
            title=milestone.title,
            description=milestone.description,
            scheduled_date=scheduled_date,
        )
        db.add(alert)
        alerts.append(alert)

    await db.commit()
    for alert in alerts:
        await db.refresh(alert)

    # Sort chronologically for the caller -- Gemini's own list order
    # (largest days_before_deadline first, per the prompt) isn't
    # guaranteed to survive the clipping above, so this re-sorts on the
    # actual final `scheduled_date` values rather than trusting order.
    alerts.sort(key=lambda a: a.scheduled_date)
    return alerts


async def get_upcoming_alerts(db: AsyncSession, user_id: UUID) -> list[DeadlineAlert]:
    """"Upcoming active alerts" per the ask -- interpreted as
    incomplete (`is_completed=False`) AND not yet in the past
    (`scheduled_date >= now`), ordered chronologically. A completed or
    already-past alert isn't something a student still needs reminding
    about."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(DeadlineAlert)
        .where(
            DeadlineAlert.user_id == user_id,
            DeadlineAlert.is_completed.is_(False),
            DeadlineAlert.scheduled_date >= now,
        )
        .order_by(DeadlineAlert.scheduled_date.asc())
    )
    return list(result.scalars().all())


async def get_tracker_alerts(db: AsyncSession, user_id: UUID, tracker_id: UUID) -> list[DeadlineAlert]:
    """All alerts (completed or not, past or future) for one tracker,
    chronologically -- used by the `.ics` export, which should include
    the whole milestone plan, not just what's still upcoming."""
    result = await db.execute(
        select(DeadlineAlert)
        .where(DeadlineAlert.user_id == user_id, DeadlineAlert.tracker_id == tracker_id)
        .order_by(DeadlineAlert.scheduled_date.asc())
    )
    return list(result.scalars().all())


async def _get_owned_alert(db: AsyncSession, user_id: UUID, alert_id: UUID) -> DeadlineAlert:
    result = await db.execute(
        select(DeadlineAlert).where(
            DeadlineAlert.id == alert_id, DeadlineAlert.user_id == user_id
        )
    )
    alert = result.scalar_one_or_none()
    if alert is None:
        raise AlertNotFoundError(str(alert_id))
    return alert


async def complete_alert(db: AsyncSession, user_id: UUID, alert_id: UUID) -> DeadlineAlert:
    alert = await _get_owned_alert(db, user_id, alert_id)
    alert.is_completed = True
    db.add(alert)
    await db.commit()
    await db.refresh(alert)
    return alert


async def delete_alert(db: AsyncSession, user_id: UUID, alert_id: UUID) -> None:
    alert = await _get_owned_alert(db, user_id, alert_id)
    await db.delete(alert)
    await db.commit()


# --- iCalendar (.ics) export -------------------------------------------

# RFC 5545 date-time form: YYYYMMDDTHHMMSSZ (the trailing "Z" means UTC,
# form 3 in section 3.3.5) -- used for DTSTAMP and DTSTART below.
_ICS_DATETIME_FORMAT = "%Y%m%dT%H%M%SZ"


def _ics_escape(text: str) -> str:
    """Escapes special characters per RFC 5545 section 3.3.11 (the TEXT
    value type): backslash, semicolon, comma, and newline. Backslash is
    escaped FIRST -- escaping the other three characters afterward would
    double-escape the backslashes those replacements just introduced."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold_line(line: str) -> str:
    """RFC 5545 section 3.1: content lines longer than 75 octets must be
    "folded" across multiple physical lines, each continuation line
    starting with a single space. Folding here is done on UTF-8 byte
    boundaries (RFC 5545 folding is explicitly octet-based, not
    character-based) while never splitting a single multi-byte character
    across two folded lines, so this stays correct for non-ASCII
    titles/descriptions too -- not just a `textwrap`-style character
    split, which could produce invalid UTF-8 mid-character."""
    if len(line.encode("utf-8")) <= 75:
        return line

    folded_parts: list[bytes] = []
    current = b""
    for char in line:
        char_bytes = char.encode("utf-8")
        if len(current) + len(char_bytes) > 75:
            folded_parts.append(current)
            current = b" " + char_bytes
        else:
            current += char_bytes
    folded_parts.append(current)
    return "\r\n".join(part.decode("utf-8") for part in folded_parts)


def export_to_ics(alerts: list[DeadlineAlert]) -> str:
    """Generates a standard RFC 5545 iCalendar string -- one VEVENT per
    alert -- via manual template strings, per the ask's explicit
    preference over adding the `icalendar` dependency.

    Each event's DTSTART is `alert.scheduled_date`; SUMMARY is
    `alert.title`; DESCRIPTION is `alert.description`. UID uses the
    alert's own UUID (already globally unique, no need to mint another
    one). Every physical line uses RFC 5545's mandatory CRLF (`\\r\\n`)
    line endings, and any line over 75 octets is folded per the spec --
    not just a minimal happy-path implementation, since a long AI-
    generated `description` is exactly the kind of content likely to
    exceed 75 octets in practice.
    """
    now_stamp = datetime.now(timezone.utc).strftime(_ICS_DATETIME_FORMAT)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Apex//Deadline Alerts//EN",
        "CALSCALE:GREGORIAN",
    ]

    for alert in alerts:
        scheduled = alert.scheduled_date
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        dtstart = scheduled.astimezone(timezone.utc).strftime(_ICS_DATETIME_FORMAT)

        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{alert.id}@apex-platform")
        lines.append(f"DTSTAMP:{now_stamp}")
        lines.append(f"DTSTART:{dtstart}")
        lines.append(_fold_line(f"SUMMARY:{_ics_escape(alert.title)}"))
        lines.append(_fold_line(f"DESCRIPTION:{_ics_escape(alert.description)}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    return "\r\n".join(lines) + "\r\n"
