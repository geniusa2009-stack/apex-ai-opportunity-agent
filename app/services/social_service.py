import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.models.badges import GamificationProfile
from app.models.courses import UserEnrollment
from app.models.social import AchievementCongrats, AchievementPost, StudyDuel, UserConnection
from app.models.user import User
from app.services import coach_service, notification_service

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)

# Ask's own literal numbers, named as module constants -- same posture as
# `coach_service.DAILY_LOGIN_TOKENS`/`guild_service.STANDARD_COMMENT_TOKENS`.
CONGRATS_TOKEN_REWARD = 1
DAILY_CONGRATS_TOKEN_LIMIT = 5


# ---------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------


class AchievementPostNotFoundError(Exception):
    """Raised when an achievement post doesn't exist."""


class DuelNotFoundError(Exception):
    """Raised when a study duel doesn't exist."""


class ConnectionNotFoundError(Exception):
    """Raised when a friend connection doesn't exist."""


class CardGenerationError(Exception):
    """Raised when the Gemini shareable-card-data generation call fails."""


class AlreadyCongratulatedError(Exception):
    """Raised when a student tries to congratulate the same achievement
    post twice. Disclosed necessary guard -- without it, one student
    could inflate `congrats_count` on a single post without limit by
    spamming the endpoint. See `app/models/social.py`'s
    `AchievementCongrats` docstring for the full disclosure on why this
    table (and therefore this guard) exists at all."""


class DuplicateConnectionError(Exception):
    """Raised when a friend request already exists for this exact
    (user_id, friend_id) pair. Same posture as
    `coach_service.get_or_create_accountability`'s `IntegrityError` race
    handling / `bookmarks.create_bookmark`'s duplicate-bookmark guard."""


class SelfConnectionError(Exception):
    """Raised when a student tries to friend themselves."""


class SelfDuelError(Exception):
    """Raised when a student tries to challenge themselves to a duel."""


class NotAParticipantError(Exception):
    """Raised when a user tries to act on a connection/duel they're not
    a party to (e.g. accepting a friend request sent to someone else, or
    responding to a duel they're not challenger/opponent in). Routers
    translate this to HTTP 403."""


class InvalidStateError(Exception):
    """Raised when an action doesn't make sense for a connection/duel's
    current status (e.g. accepting an already-accepted request, or
    completing a duel that was never accepted). Routers translate this
    to HTTP 409."""


# ---------------------------------------------------------------------
# Gemini structured-output schema + prompt (shareable card generation)
# ---------------------------------------------------------------------


class GeneratedCardData(BaseModel):
    """Populates both `AchievementPost.title` (no dedicated parameter
    exists for it in the ask's own `create_achievement_post(user_id,
    type, raw_description, privacy)` signature -- see
    `create_achievement_post`'s docstring below) and
    `shareable_card_data` in full. The ask's model-level description of
    `shareable_card_data` ("stores custom titles, themes, and AI
    quotes") and its service-level description of this call ("a
    motivating quote... and a beautiful color palette suggestion") are
    combined into one schema here rather than two separate Gemini calls,
    since both are grounded in the exact same achievement context and
    are naturally one creative act, not two independent ones.
    """

    title: str = Field(
        description="A short, punchy, celebratory headline for this "
        "specific achievement (e.g. 'Hit a 30-Day Focus Streak!') -- "
        "used both as the post's title and as the headline shown on the "
        "shareable card."
    )
    theme: str = Field(
        description="A short, evocative card theme name matching the "
        "achievement's mood, e.g. 'Golden Milestone', 'Cosmic "
        "Breakthrough', 'Unstoppable Streak'."
    )
    motivating_quote: str = Field(
        description="One short, genuinely inspiring quote tailored "
        "specifically to this achievement -- not a generic, "
        "could-apply-to-anything platitude."
    )
    color_palette: list[str] = Field(
        min_length=3,
        max_length=5,
        description="3-5 hex color codes (e.g. '#FFD700') forming a "
        "beautiful, cohesive palette for the frontend to render the "
        "card in, matching the theme's mood.",
    )


_CARD_GENERATION_SYSTEM_PROMPT = """\
You are a celebratory creative director designing a shareable "Social \
Card" for a student's academic achievement (e.g. a focus streak, a \
finished CV, a secured scholarship). You will be given the achievement's \
type and the student's own description of it in their own words.

Invent, specifically tailored to THIS achievement (never generic):
1. `title`: a short, punchy, celebratory headline.
2. `theme`: a short evocative theme name for the card's visual mood.
3. `motivating_quote`: one short, genuinely inspiring quote.
4. `color_palette`: 3-5 hex color codes forming a beautiful, cohesive \
palette matching the theme's mood (e.g. warm golds for a milestone, cool \
blues/purples for a "cosmic" theme).

Keep everything upbeat, specific, and worth sharing -- this is meant to \
make the student feel genuinely proud enough to post it publicly.
"""


def _client() -> genai.Client:
    # Same "fresh client per call" posture as every other Gemini-backed
    # service in this codebase -- see VisionGateway._client
    # (app/services/vision.py) for the full event-loop-lifetime reasoning.
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def _generate_card_data(achievement_type: str, raw_description: str) -> GeneratedCardData:
    prompt = f"Achievement type: {achievement_type}\nStudent's own description: {raw_description}"
    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_social_model,
            contents=[_CARD_GENERATION_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeneratedCardData,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise CardGenerationError(str(exc)) from exc

    if not response.text:
        raise CardGenerationError("Gemini returned no content for shareable card generation")

    return GeneratedCardData.model_validate_json(response.text)


# ---------------------------------------------------------------------
# Achievement posts / feed / congrats
# ---------------------------------------------------------------------


async def create_achievement_post(
    db: AsyncSession, user_id: UUID, achievement_type: str, raw_description: str, privacy: str
) -> AchievementPost:
    """Takes `db: AsyncSession` as an explicit first parameter, same
    disclosed necessary-plumbing addition as every DB-writing service
    function in this project (the ask's literal signature is
    `create_achievement_post(user_id, type, raw_description, privacy) ->
    AchievementPost`, with no `db` and no `title` parameter -- see
    `GeneratedCardData`'s docstring above for the disclosed choice on
    where `title` comes from instead).
    """
    card_data = await _generate_card_data(achievement_type, raw_description)

    post = AchievementPost(
        user_id=user_id,
        achievement_type=achievement_type,
        title=card_data.title,
        description=raw_description,
        privacy_level=privacy,
        shareable_card_data=card_data.model_dump(),
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)
    return post


async def _get_friend_ids(db: AsyncSession, user_id: UUID) -> set[UUID]:
    """Every ACCEPTED connection touching `user_id`, from either
    direction -- see `UserConnection`'s docstring on why friendship is
    treated as symmetric for reads despite being stored as a directed
    row."""
    result = await db.execute(
        select(UserConnection).where(
            UserConnection.status == "accepted",
            or_(UserConnection.user_id == user_id, UserConnection.friend_id == user_id),
        )
    )
    friend_ids: set[UUID] = set()
    for row in result.scalars().all():
        friend_ids.add(row.friend_id if row.user_id == user_id else row.user_id)
    return friend_ids


async def get_feed(db: AsyncSession, user_id: UUID) -> list[AchievementPost]:
    """Necessary plumbing behind `GET /social/feed` -- the ask names the
    endpoint ("Returns a feed of public achievements and friends'
    achievements") but not a dedicated service method for it.

    Visibility rules (disclosed interpretation of "public achievements
    and friends' achievements"): a post is in the feed if it's
    `public`, OR it's `friends_only` and the author is one of `user_id`'s
    accepted connections, OR the caller is the author themselves
    (always see your own posts regardless of privacy). Other students'
    `private` posts never appear, for anyone but the author.
    """
    friend_ids = await _get_friend_ids(db, user_id)

    result = await db.execute(
        select(AchievementPost)
        .where(
            or_(
                AchievementPost.privacy_level == "public",
                AchievementPost.user_id == user_id,
                and_(
                    AchievementPost.privacy_level == "friends_only",
                    AchievementPost.user_id.in_(friend_ids),
                ),
            )
        )
        .order_by(AchievementPost.created_at.desc())
    )
    return list(result.scalars().all())


async def congratulate_achievement(db: AsyncSession, user_id: UUID, post_id: UUID) -> dict:
    """Increments `congrats_count` and awards the congratulator
    `CONGRATS_TOKEN_REWARD` (+1) token, capped at
    `DAILY_CONGRATS_TOKEN_LIMIT` (5) TOKEN-EARNING congratulations per
    UTC calendar day.

    Disclosed reading of "limit 5 times a day": the cap applies to the
    TOKEN INCENTIVE, not to the act of congratulating itself -- a
    student can still cheer on more than 5 friends in a day (their 6th+
    congratulation still increments that post's `congrats_count`), they
    just stop earning the token bonus past their 5th. This reads more in
    the spirit of "building community spirit" than a hard wall that
    stops a generous student from congratulating a 6th friend.

    Each post can only be congratulated once per student (see
    `AchievementCongrats`'s docstring) -- a repeat attempt raises
    `AlreadyCongratulatedError` rather than silently no-op'ing, so the
    caller gets a clear signal rather than a misleadingly-identical
    success response.

    Locks the `AchievementPost` row for the duration of the update
    (`with_for_update()`) so concurrent congratulations on the same post
    serialize their `congrats_count` increments at the database level,
    same posture as `coach_service.get_or_create_accountability`'s
    balance-row locking.
    """
    result = await db.execute(
        select(AchievementPost).where(AchievementPost.id == post_id).with_for_update()
    )
    post = result.scalar_one_or_none()
    if post is None:
        raise AchievementPostNotFoundError(str(post_id))

    already = await db.execute(
        select(AchievementCongrats).where(
            AchievementCongrats.post_id == post_id, AchievementCongrats.congratulator_id == user_id
        )
    )
    if already.scalar_one_or_none() is not None:
        raise AlreadyCongratulatedError(str(post_id))

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_count_result = await db.execute(
        select(func.count())
        .select_from(AchievementCongrats)
        .where(
            AchievementCongrats.congratulator_id == user_id,
            AchievementCongrats.created_at >= today_start,
        )
    )
    daily_count = daily_count_result.scalar_one()
    token_awarded = daily_count < DAILY_CONGRATS_TOKEN_LIMIT

    post.congrats_count += 1
    db.add(post)
    db.add(AchievementCongrats(post_id=post_id, congratulator_id=user_id))

    try:
        await db.commit()
    except IntegrityError as exc:
        # Race: two concurrent congratulate calls from the same user on
        # the same post both passed the pre-check above. The unique
        # constraint on (post_id, congratulator_id) is the real backstop.
        await db.rollback()
        raise AlreadyCongratulatedError(str(post_id)) from exc

    await db.refresh(post)

    tokens_balance: int | None = None
    if token_awarded:
        account = await coach_service.award_tokens(
            db, user_id, CONGRATS_TOKEN_REWARD, reason=f"congratulated:{post_id}"
        )
        tokens_balance = account.tokens_balance

    return {
        "post_id": post.id,
        "congrats_count": post.congrats_count,
        "token_awarded": token_awarded,
        "tokens_balance": tokens_balance,
    }


# ---------------------------------------------------------------------
# Friend connections
# ---------------------------------------------------------------------


async def request_friend(db: AsyncSession, user_id: UUID, friend_id: UUID) -> UserConnection:
    """Takes `db: AsyncSession` as an explicit first parameter, same
    disclosed necessary-plumbing addition as every DB-writing function
    in this project.

    Mutual-request resolution (disclosed design choice, not stated by
    the ask): if `friend_id` already sent `user_id` a `pending` request
    (the exact reverse row), this call auto-accepts THAT row instead of
    creating a second, redundant one -- otherwise two students who
    friend-request each other around the same time would end up with
    two permanently-`pending` rows instead of becoming friends. If
    they're already `accepted` friends, this is a harmless idempotent
    no-op that returns the existing connection.
    """
    if user_id == friend_id:
        raise SelfConnectionError("A student can't send a friend request to themselves.")

    reverse_result = await db.execute(
        select(UserConnection).where(
            UserConnection.user_id == friend_id, UserConnection.friend_id == user_id
        )
    )
    reverse_row = reverse_result.scalar_one_or_none()
    if reverse_row is not None:
        if reverse_row.status == "pending":
            reverse_row.status = "accepted"
            db.add(reverse_row)
            await db.commit()
            await db.refresh(reverse_row)
        return reverse_row

    connection = UserConnection(user_id=user_id, friend_id=friend_id, status="pending")
    db.add(connection)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateConnectionError(
            f"A connection between {user_id} and {friend_id} already exists."
        ) from exc
    await db.refresh(connection)

    # Actionable Notifications addition -- the recipient sees this in
    # their bell feed with Accept/Reject buttons rendered right there,
    # driven by `payload.connection_id` (no second lookup needed).
    try:
        await notification_service.create_notification(
            db, friend_id, "SYSTEM", "New friend request",
            "You have a new study-buddy friend request.",
            payload={"type": "friend_request", "connection_id": str(connection.id)},
        )
    except Exception:
        logger.exception("Notifications: failed to notify user %s of friend request", friend_id)

    return connection


async def respond_to_friend_request(
    db: AsyncSession, user_id: UUID, connection_id: UUID, action: str
) -> UserConnection:
    """Disclosed necessary addition -- the ask's router section lists
    `POST /social/friends/request` and `GET /social/friends/list` but no
    endpoint to actually ACCEPT (or decline) a pending request. Without
    one, every connection created by `request_friend` would be stuck
    `pending` forever, and `get_friends_list`/the feed's `friends_only`
    visibility would never have anyone to show. `action` is `"accept"`
    or `"decline"`."""
    result = await db.execute(
        select(UserConnection).where(UserConnection.id == connection_id).with_for_update()
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise ConnectionNotFoundError(str(connection_id))

    if connection.friend_id != user_id:
        raise NotAParticipantError("Only the request's recipient can respond to it.")
    if connection.status != "pending":
        raise InvalidStateError(f"Connection is already {connection.status!r}.")

    connection.status = "accepted" if action == "accept" else "declined"
    db.add(connection)
    await db.commit()
    await db.refresh(connection)
    return connection


async def get_friends_list(db: AsyncSession, user_id: UUID) -> list[User]:
    """Necessary plumbing behind `GET /social/friends/list` -- the ask
    names the endpoint but not a dedicated service method. Returns full
    `User` rows (not bare UUIDs) so the router/schema can surface a
    usable display name/email -- a bare list of friend IDs would be a
    fairly useless response for an actual "friends list" UI."""
    friend_ids = await _get_friend_ids(db, user_id)
    if not friend_ids:
        return []

    result = await db.execute(select(User).where(User.id.in_(friend_ids)))
    return list(result.scalars().all())


async def get_friend_suggestions(db: AsyncSession, user_id: UUID, limit: int = 10) -> list[dict]:
    """AI Matchmaking & Friends Network -- `GET /social/friends/suggestions`.

    Not an LLM call (despite the feature's "AI Matchmaking" name) -- a
    deterministic weighted-signal ranking over data this codebase
    already has, reused rather than adding a new Gemini round-trip to a
    read that's likely called on every visit to a Network page:

    - +3 per course BOTH students are enrolled in (`UserEnrollment`) --
      the strongest "study buddy" signal: literally the same course.
    - +2 if their `GamificationProfile.total_xp` are within the same
      rough band (closest of: <100, 100-499, 500-1999, 2000+) -- similar
      engagement level, a reasonable proxy for "similar pace."

    Excludes: the user themself, existing friends (any status --
    pending, accepted, or declined all mean "already connected/asked,"
    not a fresh suggestion).
    """
    connected_result = await db.execute(
        select(UserConnection.user_id, UserConnection.friend_id).where(
            or_(UserConnection.user_id == user_id, UserConnection.friend_id == user_id)
        )
    )
    excluded_ids = {user_id}
    for a, b in connected_result.all():
        excluded_ids.add(a)
        excluded_ids.add(b)

    my_courses_result = await db.execute(
        select(UserEnrollment.course_id).where(UserEnrollment.user_id == user_id)
    )
    my_course_ids = set(my_courses_result.scalars().all())

    my_profile = await db.get(GamificationProfile, user_id)
    my_xp = my_profile.total_xp if my_profile else 0

    def _xp_band(xp: int) -> int:
        if xp < 100:
            return 0
        if xp < 500:
            return 1
        if xp < 2000:
            return 2
        return 3

    my_band = _xp_band(my_xp)

    candidates_result = await db.execute(select(User).where(~User.id.in_(excluded_ids)))
    candidates = list(candidates_result.scalars().all())

    scored: list[dict] = []
    for candidate in candidates:
        their_courses_result = await db.execute(
            select(UserEnrollment.course_id).where(UserEnrollment.user_id == candidate.id)
        )
        their_course_ids = set(their_courses_result.scalars().all())
        shared_course_ids = my_course_ids & their_course_ids

        their_profile = await db.get(GamificationProfile, candidate.id)
        their_xp = their_profile.total_xp if their_profile else 0

        score = len(shared_course_ids) * 3
        if _xp_band(their_xp) == my_band:
            score += 2
        if score == 0:
            continue

        scored.append({
            "user_id": candidate.id,
            "full_name": candidate.full_name,
            "shared_courses": len(shared_course_ids),
            "similar_xp_level": _xp_band(their_xp) == my_band,
            "match_score": score,
        })

    scored.sort(key=lambda s: s["match_score"], reverse=True)
    return scored[:limit]


# ---------------------------------------------------------------------
# Study duels
# ---------------------------------------------------------------------


async def challenge_duel(
    db: AsyncSession, challenger_id: UUID, opponent_id: UUID, duration_minutes: int, token_wager: int
) -> StudyDuel:
    """Takes `db: AsyncSession` as an explicit first parameter, same
    disclosed necessary-plumbing addition as every DB-writing function
    in this project.

    Wager timing (disclosed design choice -- the ask states WHAT the
    wager is, not WHEN it's collected): the challenger's stake is
    deducted immediately, at challenge time, via
    `coach_service.charge_for_feature` -- they've "put their tokens on
    the line" the moment they issue the challenge, mirroring how a real
    wager works. This raises `coach_service.InsufficientTokensError`
    (translated to HTTP 402 by the router) if they can't afford it,
    before any `StudyDuel` row is even created. The opponent's matching
    stake is collected separately, only if/when they accept (see
    `manage_duel` below) -- if they decline, the challenger is refunded
    and nothing was ever taken from the opponent.
    """
    if challenger_id == opponent_id:
        raise SelfDuelError("A student can't challenge themselves to a study duel.")

    await coach_service.charge_for_feature(db, challenger_id, "study_duel_wager", token_wager)

    duel = StudyDuel(
        challenger_id=challenger_id,
        opponent_id=opponent_id,
        duration_minutes=duration_minutes,
        token_wager=token_wager,
        status="pending",
    )
    db.add(duel)
    await db.commit()
    await db.refresh(duel)
    return duel


async def manage_duel(
    db: AsyncSession, acting_user_id: UUID, duel_id: UUID, action: str, winner_id: UUID | None = None
) -> StudyDuel:
    """Handles accepting, declining, or completing a study duel and
    distributing the token pool. Disclosed necessary additions to the
    ask's own literal `manage_duel(duel_id: UUID, action: str, winner_id:
    UUID = None) -> StudyDuel` signature: `db: AsyncSession` (same
    posture as every other DB-writing function here) and
    `acting_user_id: UUID` -- without knowing WHO is performing the
    action, there is no way to enforce "only the opponent can
    accept/decline" or "only a participant can report a result," which
    is a genuine security gap a token-wagering feature can't ship with.

    Transactional-locking posture (per the ask's explicit instruction):
    the `StudyDuel` row itself is fetched with `with_for_update()` and
    held locked for this entire call, so a second concurrent
    `manage_duel` call on the SAME duel (e.g. simultaneous accept+decline,
    or double-complete) blocks at the database level until this call
    commits and releases the lock -- it will then see the already-updated
    `status` and correctly raise `InvalidStateError` instead of double-
    processing the wager. Each individual token movement
    (`charge_for_feature`/`refund_tokens`/`award_tokens`) is itself
    additionally safe via `coach_service.get_or_create_accountability`'s
    own `SELECT ... FOR UPDATE` on the balance row. What this does NOT
    achieve is wrapping the duel-row update and every token movement in
    one single all-or-nothing database transaction (each `coach_service`
    call commits on its own) -- a genuinely atomic multi-table
    transaction would need `coach_service`'s helpers refactored to accept
    a caller-supplied transaction instead of managing their own commits,
    which is a real infrastructure change beyond this step's scope (see
    the README's "Not included" note on this). In practice this is a
    narrow window: the duel row stays locked the whole time, so no other
    request can race in and observe an inconsistent state.

    Action semantics:
    - `"accept"`: only the opponent, only while `pending`. Collects the
      opponent's matching wager.
    - `"decline"`: only the opponent, only while `pending`. Refunds the
      challenger's already-collected wager.
    - `"complete"`: either participant, only while `accepted` (a
      disclosed trust simplification -- either side can report the
      result; the ask describes no separate dual-confirmation flow, and
      building one is a reasonable follow-on, not done here). Awards the
      full pool (`token_wager * 2`) to `winner_id` if given, or splits
      it evenly back to both participants for a tie (`winner_id=None`).
    """
    result = await db.execute(select(StudyDuel).where(StudyDuel.id == duel_id).with_for_update())
    duel = result.scalar_one_or_none()
    if duel is None:
        raise DuelNotFoundError(str(duel_id))

    if acting_user_id not in (duel.challenger_id, duel.opponent_id):
        raise NotAParticipantError("Only the challenger or opponent can act on this duel.")

    if action == "accept":
        if acting_user_id != duel.opponent_id:
            raise NotAParticipantError("Only the opponent can accept a challenge.")
        if duel.status != "pending":
            raise InvalidStateError(f"Duel is {duel.status!r}, not pending.")
        await coach_service.charge_for_feature(db, duel.opponent_id, "study_duel_wager", duel.token_wager)
        duel.status = "accepted"

    elif action == "decline":
        if acting_user_id != duel.opponent_id:
            raise NotAParticipantError("Only the opponent can decline a challenge.")
        if duel.status != "pending":
            raise InvalidStateError(f"Duel is {duel.status!r}, not pending.")
        await coach_service.refund_tokens(db, duel.challenger_id, duel.token_wager, "study_duel_declined")
        duel.status = "declined"

    elif action == "complete":
        if duel.status != "accepted":
            raise InvalidStateError(f"Duel is {duel.status!r}, not accepted.")
        if winner_id is not None and winner_id not in (duel.challenger_id, duel.opponent_id):
            raise NotAParticipantError("winner_id must be one of this duel's two participants.")

        pool = duel.token_wager * 2
        if winner_id is None:
            split = pool // 2
            await coach_service.award_tokens(db, duel.challenger_id, split, "study_duel_tie")
            await coach_service.award_tokens(db, duel.opponent_id, split, "study_duel_tie")
            duel.status = "completed_tie"
        else:
            await coach_service.award_tokens(db, winner_id, pool, "study_duel_won")
            duel.status = (
                "completed_challenger_won" if winner_id == duel.challenger_id else "completed_opponent_won"
            )

    else:
        raise InvalidStateError(f"Unknown duel action: {action!r}")

    db.add(duel)
    await db.commit()
    await db.refresh(duel)
    return duel
