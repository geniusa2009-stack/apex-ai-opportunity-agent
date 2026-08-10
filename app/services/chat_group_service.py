"""Auto-Assigned English Practice Groups -- capacity-aware matchmaking +
the 48-hour inactivity sweep.

Net-new feature, built on top of the pre-existing English-Only Gamified
Real-time Chat schema (`app/models/chat.py`) and its `chat_service.py`
persistence helpers, which this module deliberately reuses (`ChatRoom`,
`ChatParticipant`) rather than duplicating. Kept in its own file, not
appended to `chat_service.py`, because it is a genuinely separate
concern (system-managed matchmaking + a scheduled sweep) from that
file's own "rooms/messages a user explicitly created" scope -- same
"one file per distinct feature" posture already established across this
codebase (e.g. `live_session_service.py` living alongside, not inside,
`mentorship`'s own service module).

DISCLOSED PRODUCT PARAMETERS (not literally specified anywhere else in
this codebase -- chosen here, openly, as the two numbers this feature
needs to exist at all):
- `PRACTICE_GROUP_MAX_MEMBERS = 30` -- the capacity cap named directly
  by this feature's own ask ("Max 30 members").
- `INACTIVITY_THRESHOLD_HOURS = 48` -- the sweep window named directly
  by this feature's own ask ("inactive for 48 hours").
"""

import datetime as dt
import logging
import uuid

from google import genai
from google.genai import errors as genai_errors
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.models.chat import ChatMessage, ChatParticipant, ChatRoom
from app.models.user import User

logger = logging.getLogger(__name__)

PRACTICE_GROUP_MAX_MEMBERS = 30
INACTIVITY_THRESHOLD_HOURS = 48

_PRACTICE_GROUP_NAME = "English Practice Group"

# --- AI-Powered Course Cohorts ---
# A question with no reply within this window gets an auto-generated AI
# Bot answer -- this feature's own literal "no replies for 2 hours"
# window.
STALE_QUESTION_THRESHOLD_HOURS = 2
AI_BOT_EMAIL = "ai-bot@apex.internal"
AI_BOT_NAME = "APEX AI Bot"

_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


async def _member_count(db: AsyncSession, room_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count()).select_from(ChatParticipant).where(ChatParticipant.room_id == room_id)
    )
    return result.scalar_one()


async def assign_user_to_practice_group(db: AsyncSession, user_id: uuid.UUID) -> tuple[ChatRoom, bool]:
    """Adds `user_id` to an under-capacity `is_auto_assigned` room, or
    creates a brand new one if none has room. Returns `(room, created)`
    so the caller (the `/chat/practice-groups/join` endpoint) can tell
    the frontend whether this request started a fresh group.

    Idempotent: a user already a member of SOME auto-assigned room is
    returned that same room unchanged, rather than being added to a
    second one -- a student only ever belongs to one practice group at a
    time, matching the feature's own "assigned to a group" framing (not
    "assigned to N groups").

    Capacity check + insert happen inside one transaction; a real
    concurrent-signup race (two users both reading "29/30" at once and
    both joining, landing at 31) is a disclosed, accepted, narrow race
    for this pass -- a hard guarantee would need `SELECT ... FOR UPDATE`
    on the room row, which this feature's own ask has no stated need for
    (a soft, best-effort cap on a social study-group feature, not a
    payment/inventory system).
    """
    existing_result = await db.execute(
        select(ChatRoom)
        .join(ChatParticipant, ChatParticipant.room_id == ChatRoom.id)
        .where(ChatRoom.is_auto_assigned.is_(True), ChatParticipant.user_id == user_id)
    )
    existing_room = existing_result.scalars().first()
    if existing_room is not None:
        return existing_room, False

    candidates_result = await db.execute(
        select(ChatRoom)
        .where(ChatRoom.is_auto_assigned.is_(True))
        .order_by(ChatRoom.created_at.asc())
    )
    for room in candidates_result.scalars().all():
        count = await _member_count(db, room.id)
        if count < (room.max_members or PRACTICE_GROUP_MAX_MEMBERS):
            now = dt.datetime.now(dt.timezone.utc)
            db.add(ChatParticipant(room_id=room.id, user_id=user_id, last_active_at=now))
            await db.commit()
            await db.refresh(room)
            return room, False

    room = ChatRoom(
        name=_PRACTICE_GROUP_NAME,
        is_group=True,
        is_auto_assigned=True,
        max_members=PRACTICE_GROUP_MAX_MEMBERS,
    )
    db.add(room)
    await db.flush()
    db.add(ChatParticipant(room_id=room.id, user_id=user_id))
    await db.commit()
    await db.refresh(room)
    return room, True


async def touch_participant_activity(db: AsyncSession, room_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Updates `last_active_at` for one (room, user) pair -- called from
    the chat WebSocket handler alongside `chat_service.save_message`,
    every time a participant successfully sends a message. Only matters
    for `is_auto_assigned` rooms (see `sweep_inactive_participants`
    below), but harmlessly updated for every room a message is sent in,
    rather than adding a conditional the caller has to get right."""
    participant = await db.get(ChatParticipant, (room_id, user_id))
    if participant is not None:
        participant.last_active_at = dt.datetime.now(dt.timezone.utc)
        await db.commit()


async def set_pinned(db: AsyncSession, room_id: uuid.UUID, user_id: uuid.UUID, pinned: bool) -> ChatParticipant | None:
    """Sets this user's own `is_pinned` flag on their membership row.
    Returns `None` if the caller isn't actually a participant of this
    room (the router maps that to a 404/403, not a silent no-op)."""
    participant = await db.get(ChatParticipant, (room_id, user_id))
    if participant is None:
        return None
    participant.is_pinned = pinned
    await db.commit()
    await db.refresh(participant)
    return participant


async def sweep_inactive_participants(
    db: AsyncSession, threshold_hours: int = INACTIVITY_THRESHOLD_HOURS
) -> dict[str, int]:
    """Removes every participant of an `is_auto_assigned` room whose own
    `last_active_at` is older than `threshold_hours` -- this feature's
    own literal "sweep and remove members inactive for 48 hours."

    Scoped to `is_auto_assigned` rooms only: a user-created group/DM has
    no capacity to free up and no matchmaking fairness concern, so
    sweeping it would just be silently kicking a friend out of a private
    conversation for going quiet -- not this feature's job. Removal
    (not e.g. a `status` flag) is deliberate: once removed, a swept
    student's NEXT `/chat/practice-groups/join` call naturally lands
    them in a fresh or different under-capacity group, satisfying the
    ask's own "avoid being moved [i.e. get moved elsewhere]" framing.

    Returns a small summary dict (`rooms_checked`/`participants_removed`)
    -- same "meaningful return value, not a bare None" posture as every
    other periodic-task body in this codebase
    (`deadline_reminder_tasks._check_approaching_deadlines`).
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=threshold_hours)

    rooms_result = await db.execute(select(ChatRoom.id).where(ChatRoom.is_auto_assigned.is_(True)))
    room_ids = list(rooms_result.scalars().all())

    removed = 0
    for room_id in room_ids:
        stale_result = await db.execute(
            select(ChatParticipant).where(
                ChatParticipant.room_id == room_id,
                ChatParticipant.last_active_at < cutoff,
            )
        )
        stale_participants = list(stale_result.scalars().all())
        for participant in stale_participants:
            await db.delete(participant)
            removed += 1
        if stale_participants:
            await db.commit()

    return {"rooms_checked": len(room_ids), "participants_removed": removed}


# ---------------------------------------------------------------------
# AI-Powered Course Cohorts (Auto-Groups)
# ---------------------------------------------------------------------


async def get_or_create_course_cohort_room(db: AsyncSession, course_id: uuid.UUID, course_title: str) -> ChatRoom:
    """One `ChatRoom` per course, created lazily on first enrollment
    (`course_service.enroll_in_course` calls `join_course_cohort` below,
    which calls this). Uncapped -- `max_members` stays `None`, unlike
    English Practice groups, since a course cohort's whole point is
    every enrolled student being able to join, not a soft-capped
    matchmaking pool."""
    existing = await db.execute(select(ChatRoom).where(ChatRoom.course_id == course_id))
    room = existing.scalars().first()
    if room is not None:
        return room

    room = ChatRoom(name=f"{course_title} — Cohort", is_group=True, course_id=course_id)
    db.add(room)
    await db.commit()
    await db.refresh(room)
    return room


async def join_course_cohort(db: AsyncSession, course_id: uuid.UUID, course_title: str, user_id: uuid.UUID) -> ChatRoom:
    """Auto-joins `user_id` into their course's cohort room -- called
    right after a successful `UserEnrollment` insert. Idempotent: a
    student re-calling enroll (already enrolled) simply finds their
    existing membership and changes nothing."""
    room = await get_or_create_course_cohort_room(db, course_id, course_title)
    participant = await db.get(ChatParticipant, (room.id, user_id))
    if participant is None:
        db.add(ChatParticipant(room_id=room.id, user_id=user_id))
        await db.commit()
    return room


async def set_muted(db: AsyncSession, room_id: uuid.UUID, user_id: uuid.UUID, muted: bool) -> ChatParticipant | None:
    """Mute/unmute a room for this user only -- independent of leaving
    it (`leave` below) or of `set_pinned`."""
    participant = await db.get(ChatParticipant, (room_id, user_id))
    if participant is None:
        return None
    participant.is_muted = muted
    await db.commit()
    await db.refresh(participant)
    return participant


async def leave_cohort(db: AsyncSession, room_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Removes this user's membership row -- the group header's "Leave"
    action. Returns `False` if they weren't a member to begin with (the
    router maps that to a 404), rather than raising."""
    participant = await db.get(ChatParticipant, (room_id, user_id))
    if participant is None:
        return False
    await db.delete(participant)
    await db.commit()
    return True


async def _get_or_create_ai_bot_user(db: AsyncSession) -> User:
    """Lazily provisions the single system `User` row every AI Bot
    message's `sender_id` points at -- `ChatMessage.sender_id` is a
    plain, `NOT NULL` FK (see that model's own docstring), so there is
    no nullable-sender option; a real, dedicated system account is the
    disclosed necessary way to satisfy that constraint for a
    machine-authored message."""
    result = await db.execute(select(User).where(User.email == AI_BOT_EMAIL))
    bot = result.scalar_one_or_none()
    if bot is not None:
        return bot

    bot = User(id=uuid.uuid4(), email=AI_BOT_EMAIL, full_name=AI_BOT_NAME, role="bot")
    db.add(bot)
    await db.commit()
    await db.refresh(bot)
    return bot


def _bot_client() -> genai.Client:
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def _generate_bot_answer(question: str) -> str:
    prompt = (
        "You are the APEX AI Bot, a friendly, concise course-cohort study assistant. "
        "A student in a course chat group asked a question and no classmate has replied "
        "within 2 hours. Give a short, genuinely helpful answer (2-4 sentences), in "
        f"English. Question: {question}"
    )
    response = await _bot_client().aio.models.generate_content(
        model=settings.gemini_coach_model, contents=[prompt]
    )
    return response.text or "I don't have a confident answer for that yet -- try asking your mentor!"


async def answer_stale_course_questions(
    db: AsyncSession, threshold_hours: int = STALE_QUESTION_THRESHOLD_HOURS
) -> dict[str, int]:
    """Scans every course-cohort room (`course_id IS NOT NULL`) for a
    message that (a) ends in "?" (the cheapest, disclosed heuristic for
    "this looks like a question" -- no NLP classifier is warranted for
    this pass), (b) is older than `threshold_hours`, and (c) has no
    LATER message in the same room from anyone else -- i.e. genuinely
    unanswered, not just old. Posts one AI Bot reply per stale question
    found.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=threshold_hours)

    rooms_result = await db.execute(select(ChatRoom.id).where(ChatRoom.course_id.isnot(None)))
    room_ids = list(rooms_result.scalars().all())

    answered = 0
    for room_id in room_ids:
        latest_msg_result = await db.execute(
            select(ChatMessage).where(ChatMessage.room_id == room_id).order_by(ChatMessage.created_at.desc()).limit(1)
        )
        latest = latest_msg_result.scalar_one_or_none()
        if latest is None or latest.is_bot or not latest.content.strip().endswith("?"):
            continue
        if latest.created_at >= cutoff:
            continue

        try:
            answer_text = await _generate_bot_answer(latest.content)
        except Exception:
            logger.exception("Course Cohorts: AI Bot failed to answer stale question in room %s", room_id)
            continue

        bot = await _get_or_create_ai_bot_user(db)
        db.add(ChatMessage(room_id=room_id, sender_id=bot.id, content=answer_text, is_bot=True))
        await db.commit()
        answered += 1

    return {"rooms_checked": len(room_ids), "questions_answered": answered}
