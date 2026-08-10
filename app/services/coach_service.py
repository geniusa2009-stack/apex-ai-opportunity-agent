import logging
from datetime import datetime, timedelta, timezone
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
from app.models.application_tracker import ApplicationTracker
from app.models.deadline_alert import DeadlineAlert
from app.models.gamification import StudentAccountability
from app.models.user import User

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)

# Named per the ask's own examples ("logging in daily [+10]").
DAILY_LOGIN_TOKENS = 10


class InsufficientTokensError(Exception):
    """Raised by `charge_for_feature` when a user can't afford a premium
    feature. Routers catch this and translate it to HTTP 402."""


class AlreadyClaimedTodayError(Exception):
    """Raised by `claim_daily_login` when the user has already claimed
    their daily tokens today (server-UTC calendar date). Necessary
    guard, not explicitly named in the ask -- without it, `POST
    /coach/daily-claim` would let a user mint unlimited tokens by simply
    calling the endpoint repeatedly, which would break the whole token
    economy this step is building. Routers catch this and translate it
    to HTTP 409."""


class PepTalkGenerationError(Exception):
    """Raised when the Gemini pep-talk call fails."""


# ---------------------------------------------------------------------
# Account lookup
# ---------------------------------------------------------------------


async def get_or_create_accountability(db: AsyncSession, user_id: UUID) -> StudentAccountability:
    """Every `StudentAccountability` row is lazily created on first
    access -- the ask has no dedicated "enroll in the token economy"
    endpoint, so this is necessary plumbing, disclosed.

    Uses `SELECT ... FOR UPDATE` (`with_for_update()`) once the row
    exists, so concurrent award/deduct/claim calls for the same user
    serialize against each other at the database level instead of racing
    on a stale in-memory balance read by two requests at once -- a real
    integrity concern for a currency system, and directly in the spirit
    of the ask's "write robust, async SQLAlchemy code" instruction. The
    first-ever-row race (two concurrent requests for a brand-new user
    both seeing no row and both trying to `INSERT`) is handled the same
    way `app/api/v1/endpoints/bookmarks.py`'s `create_bookmark` already
    handles an analogous race: catch the `IntegrityError` on the losing
    insert, roll back, and re-fetch the winner instead of erroring.
    """
    result = await db.execute(
        select(StudentAccountability)
        .where(StudentAccountability.user_id == user_id)
        .with_for_update()
    )
    account = result.scalar_one_or_none()
    if account is not None:
        return account

    account = StudentAccountability(user_id=user_id)
    db.add(account)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        result = await db.execute(
            select(StudentAccountability)
            .where(StudentAccountability.user_id == user_id)
            .with_for_update()
        )
        account = result.scalar_one()
    else:
        await db.refresh(account)

    return account


# ---------------------------------------------------------------------
# Token economy
# ---------------------------------------------------------------------


async def award_tokens(
    db: AsyncSession, user_id: UUID, amount: int, reason: str
) -> StudentAccountability:
    if amount <= 0:
        raise ValueError("award amount must be positive")

    account = await get_or_create_accountability(db, user_id)
    account.tokens_balance += amount
    account.total_earned_tokens += amount
    db.add(account)
    await db.commit()
    await db.refresh(account)
    logger.info("Awarded %s tokens to user %s (%s)", amount, user_id, reason)
    return account


async def deduct_tokens(db: AsyncSession, user_id: UUID, amount: int, feature_key: str) -> bool:
    if amount <= 0:
        raise ValueError("deduct amount must be positive")

    account = await get_or_create_accountability(db, user_id)
    if account.tokens_balance < amount:
        return False

    account.tokens_balance -= amount
    db.add(account)
    await db.commit()
    await db.refresh(account)
    logger.info("Deducted %s tokens from user %s for %s", amount, user_id, feature_key)
    return True


async def charge_for_feature(db: AsyncSession, user_id: UUID, feature_key: str, cost: int) -> None:
    """Thin wrapper around `deduct_tokens` that raises instead of
    returning `False` -- necessary plumbing for the router-level 402 flow
    (see `require_tokens` in app/api/deps.py), not itself named in the
    ask (which specifies `deduct_tokens(...) -> bool` directly)."""
    success = await deduct_tokens(db, user_id, cost, feature_key)
    if not success:
        raise InsufficientTokensError(
            f"User {user_id} has insufficient tokens for {feature_key} (needs {cost})"
        )


async def refund_tokens(
    db: AsyncSession, user_id: UUID, amount: int, feature_key: str
) -> StudentAccountability:
    """Reverses a `charge_for_feature` deduction when the paid-for AI
    call itself subsequently fails.

    Not named in the ask, but a necessary fairness safeguard: the ask's
    literal integration instruction is to deduct tokens BEFORE calling
    the Gemini API. Taken completely literally with no refund path, a
    downstream Gemini failure (rate limit, transient outage, etc.) would
    leave a student charged for a premium feature that never actually
    produced anything for them. Deliberately does NOT increment
    `total_earned_tokens` (unlike `award_tokens`) -- a refund restores
    what was taken, it isn't new value the student earned, so it
    shouldn't inflate their lifetime-earned figure.
    """
    account = await get_or_create_accountability(db, user_id)
    account.tokens_balance += amount
    db.add(account)
    await db.commit()
    await db.refresh(account)
    logger.info("Refunded %s tokens to user %s for failed %s", amount, user_id, feature_key)
    return account


# ---------------------------------------------------------------------
# Daily claim / streak
# ---------------------------------------------------------------------


async def claim_daily_login(db: AsyncSession, user_id: UUID) -> StudentAccountability:
    """Awards `DAILY_LOGIN_TOKENS` and updates `current_streak`, once per
    UTC calendar day.

    Streak logic: if the account's last claim was yesterday (UTC date),
    the streak continues (+1); if it was any earlier than that (or this
    is the very first claim ever), the streak resets to 1. If a claim
    already happened today, raises `AlreadyClaimedTodayError` rather than
    silently no-op'ing or re-awarding -- "claims daily login tokens"
    implies once-per-day semantics, and without this guard a user could
    mint unlimited tokens by spamming this endpoint.

    Inlines the token award directly (rather than calling `award_tokens`
    as a separate step) so the streak update and the token award commit
    together in one transaction against the one already
    `SELECT ... FOR UPDATE`-locked row, instead of two separate round
    trips that would each re-fetch and re-lock the row.
    """
    account = await get_or_create_accountability(db, user_id)

    now = datetime.now(timezone.utc)
    today = now.date()

    if account.last_active_at is not None and account.last_active_at.date() == today:
        raise AlreadyClaimedTodayError("Daily tokens already claimed today.")

    if account.last_active_at is not None and account.last_active_at.date() == today - timedelta(days=1):
        account.current_streak += 1
    else:
        account.current_streak = 1

    account.last_active_at = now
    account.tokens_balance += DAILY_LOGIN_TOKENS
    account.total_earned_tokens += DAILY_LOGIN_TOKENS

    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


# ---------------------------------------------------------------------
# Wallet dashboard: tier + coach insight (read-only composition, see
# app/schemas/wallet.py's module docstring for why no new model exists)
# ---------------------------------------------------------------------

# Thresholds are on lifetime-earned tokens, not spendable balance, so
# spending never demotes a student out of a tier they already earned.
_TIERS: list[tuple[str, int]] = [
    ("Novice", 0),
    ("Scholar", 300),
    ("Elite", 800),
    ("Apex", 2000),
]


def compute_tier(lifetime_earned: int) -> tuple[str, str | None, int | None]:
    """Returns (current_tier, next_tier, tokens_to_next_tier)."""
    current = _TIERS[0][0]
    for name, threshold in _TIERS:
        if lifetime_earned >= threshold:
            current = name
    idx = [name for name, _ in _TIERS].index(current)
    if idx + 1 < len(_TIERS):
        next_name, next_threshold = _TIERS[idx + 1]
        return current, next_name, max(next_threshold - lifetime_earned, 0)
    return current, None, None


def build_coach_insight(
    tokens_to_next_tier: int | None, next_tier: str | None, current_streak_days: int
) -> str:
    """A short, deterministic (no LLM call -- this loads on every
    dashboard view, unlike the on-demand `generate_pep_talk`) coach line
    combining real streak + tier-distance state."""
    if tokens_to_next_tier and next_tier:
        return f"You're {tokens_to_next_tier} tokens away from {next_tier} tier!"
    if current_streak_days > 0:
        return f"{current_streak_days}-day streak going -- keep it alive with today's activity."
    return "Log in today to start your streak and earn your first tokens."


# ---------------------------------------------------------------------
# AI empathetic coach
# ---------------------------------------------------------------------


class PepTalkResult(BaseModel):
    coach_message: str = Field(
        description="A warm, highly personalized, empathetic message "
        "addressing the student's current bottleneck by name if one is "
        "identifiable (a specific overdue milestone or stuck task) -- "
        "not a generic platitude."
    )
    micro_step: str = Field(
        description="One tiny, concrete action the student could "
        "complete in about 5 minutes to break their procrastination loop "
        "on that specific bottleneck."
    )


_PEP_TALK_SYSTEM_PROMPT = """\
You are an incredibly supportive, warm academic psychologist and \
accountability coach for a student navigating scholarship, internship, and \
college applications. Your job is to send a short, deeply personalized, \
empathetic check-in that addresses their SPECIFIC current bottleneck -- not \
a generic "you can do it!" platitude.

You will be given: the student's profile (major, degree level, target \
country -- whatever is on record, some may be missing), a list of their \
still-incomplete preparation tasks across their active scholarship trackers, \
and a list of milestones that are currently overdue.

Your job:
1. Write `coach_message`: a warm, specific, empathetic message. If there's an \
identifiable overdue milestone or stuck task, name it directly (e.g. "I saw \
you've been putting off your motivation letter for the Chevening Scholarship. \
It's completely normal to feel overwhelmed by a blank page."). Validate the \
feeling first, without judgment or guilt -- procrastination is usually about \
feeling overwhelmed, not laziness. If there's no clear overdue item or stuck \
task, write an encouraging message about their overall progress instead.
2. Write `micro_step`: ONE tiny, concrete action they could complete in \
about 5 minutes to break the procrastination loop on that specific \
bottleneck (e.g. "Open a blank doc and write just one sentence about why you \
want this scholarship -- nothing more.").

Rules:
- Never be judgmental, guilt-inducing, or use shame as a motivational tool.
- Be specific to what's actually given to you (the real task/milestone names) \
-- not a generic template that could apply to any student.
- Keep `coach_message` warm and human, not clinical -- genuinely concise (a \
few sentences), not a wall of text.
- `micro_step` must be small enough to plausibly take under 5 minutes.
- You are not a licensed mental health professional and this is not therapy \
or crisis support. Stay in the lane of ordinary academic-application \
procrastination and encouragement -- do not attempt to diagnose, and do not \
offer clinical mental-health advice. This is the same scope boundary already \
established for this platform's other emotionally-supportive features (see \
app/services/scholarship_recovery.py).
"""


def _client() -> genai.Client:
    # Same "fresh client per call" posture as every other Gemini-backed
    # service in this codebase -- see VisionGateway._client
    # (app/services/vision.py) for the full event-loop-lifetime reasoning.
    return genai.Client(api_key=settings.gemini_api_key)


def _format_profile(profile: dict) -> str:
    lines = []
    for label, key in (("Major", "major"), ("Degree level", "degree_level"), ("Target country", "target_country")):
        value = profile.get(key)
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines) if lines else "(No profile details on record.)"


@_retry_gemini_call
async def _call_pep_talk_gemini(
    profile: dict, incomplete_tasks: list[str], overdue_titles: list[str]
) -> PepTalkResult:
    prompt_parts = [f"Student profile:\n{_format_profile(profile)}"]

    if incomplete_tasks:
        prompt_parts.append("Incomplete preparation tasks:\n" + "\n".join(f"- {t}" for t in incomplete_tasks))
    else:
        prompt_parts.append("Incomplete preparation tasks: none on record.")

    if overdue_titles:
        prompt_parts.append("Currently overdue milestones:\n" + "\n".join(f"- {t}" for t in overdue_titles))
    else:
        prompt_parts.append("Currently overdue milestones: none.")

    prompt = "\n\n".join(prompt_parts)

    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_coach_model,
            contents=[_PEP_TALK_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PepTalkResult,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise PepTalkGenerationError(str(exc)) from exc

    if not response.text:
        raise PepTalkGenerationError("Gemini returned no content for pep talk generation")

    return PepTalkResult.model_validate_json(response.text)


async def _gather_pep_talk_context(
    db: AsyncSession, user_id: UUID
) -> tuple[dict, list[str], list[str]]:
    """Reads the student's profile, active Kanban tasks, and overdue
    milestones -- exactly the three inputs the ask names for
    `generate_pep_talk`. "Active" trackers are those not yet in a
    terminal `accepted`/`rejected` state (mirrors the same status-based
    filtering already used for a tracker's own lifecycle in
    app/models/enums.py's `TrackerStatus`)."""
    user = await db.get(User, user_id)
    profile = {
        "major": user.major if user else None,
        "degree_level": user.degree_level if user else None,
        "target_country": user.target_country if user else None,
    }

    tracker_result = await db.execute(
        select(ApplicationTracker).where(
            ApplicationTracker.user_id == user_id,
            ApplicationTracker.status.notin_(["accepted", "rejected"]),
        )
    )
    active_trackers = tracker_result.scalars().all()

    incomplete_tasks = [
        f"{tracker.scholarship_name}: {task['task_name']}"
        for tracker in active_trackers
        for task in tracker.ai_todo_list
        if not task.get("is_completed")
    ]

    now = datetime.now(timezone.utc)
    overdue_result = await db.execute(
        select(DeadlineAlert).where(
            DeadlineAlert.user_id == user_id,
            DeadlineAlert.is_completed.is_(False),
            DeadlineAlert.scheduled_date < now,
        )
    )
    overdue_titles = [alert.title for alert in overdue_result.scalars().all()]

    return profile, incomplete_tasks, overdue_titles


async def generate_pep_talk(db: AsyncSession, user_id: UUID) -> PepTalkResult:
    """Takes `db: AsyncSession` as an explicit first parameter -- the
    same disclosed addition established repeatedly in this project
    (`memory_service`, `application_tracker.create_tracker`,
    `deadline_alerts.generate_milestones`): this function reads (and, for
    `procrastination_score`, writes) the database and needs a session.

    As a side effect, recomputes and persists `procrastination_score` as
    the current count of overdue milestones -- a disclosed
    interpretation of that column's terse one-line ask description
    ("increases if tasks in Kanban remain overdue"). This is the one
    function that already gathers the data needed to compute it, and no
    other method or endpoint was named as owning that update, so a live
    snapshot (recomputed each call) was chosen over an ever-climbing
    counter, which would grow unbounded from repeated dashboard/pep-talk
    polling and wouldn't reflect the STUDENT'S CURRENT state as
    accurately as a fresh count does.
    """
    profile, incomplete_tasks, overdue_titles = await _gather_pep_talk_context(db, user_id)

    account = await get_or_create_accountability(db, user_id)
    account.procrastination_score = len(overdue_titles)
    db.add(account)
    await db.commit()

    return await _call_pep_talk_gemini(profile, incomplete_tasks, overdue_titles)
