"""Ecosystem Features 11 & 12: AI-Powered Social Hub, Share Cards, and
Study Duels.

Three models come straight from this step's own ask (`UserConnection`,
`AchievementPost`, `StudyDuel`); a fourth, `AchievementCongrats`, is this
project's own necessary addition -- see its docstring below for why.
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserConnection(Base):
    """A directed friendship/follow edge between two students.

    `id`: the ask's field list for this model never mentions a primary
    key at all (unlike, say, `StudentAccountability`'s explicit "PK on
    user_id" from Feature 4, which really was a deliberate 1-row-per-user
    exception). A student can have many connections, so this needs its
    own row identity the same way every other many-per-user table in
    this codebase gets one (`GuildPost`, `DeadlineAlert`, etc.) --
    disclosed necessary addition, not a literal ask field.

    Row shape: `user_id` is whoever SENT the request, `friend_id` is the
    recipient. `status` starts `pending` and is flipped to `accepted` or
    `declined` by the recipient (see `social_service.respond_to_friend_request`).
    Once `accepted`, the pair is treated as a symmetric friendship for
    every read in this feature (feed visibility, friends list) --
    checked in both directions, not just `user_id == me`.

    `UniqueConstraint` on `(user_id, friend_id)` is a disclosed necessary
    guard -- without it, nothing stops the same student from firing off
    the same friend request repeatedly and ending up with duplicate rows
    for one relationship. It does NOT stop the mirror-direction row
    (B requesting A after A already requested B) -- resolving that
    "both requested each other" edge case is left to `social_service`
    (see its docstring) rather than a DB constraint, since it's a
    business rule, not a data-integrity one.
    """

    __tablename__ = "user_connections"
    __table_args__ = (UniqueConstraint("user_id", "friend_id", name="uq_user_connections_pair"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    friend_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Ask writes this as "e.g., pending, accepted" -- non-exhaustive
    # wording, same posture as `parent_pitch.primary_concern` -- so this
    # stays a plain String rather than a locked enum. In practice this
    # codebase only ever writes `pending`, `accepted`, or `declined` (the
    # third value is a disclosed necessary addition -- a request has to
    # be decline-able, or a rejected request would stay permanently
    # `pending` forever with no way to clear it).
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<UserConnection id={self.id} user_id={self.user_id} friend_id={self.friend_id} status={self.status!r}>"


class AchievementPost(Base):
    """A "Wall of Pride" post -- fully specified by this step's own ask.

    `achievement_type` is written with "e.g." in the ask (non-exhaustive
    examples), so it's a plain String, not a locked enum -- same posture
    as `parent_pitch.primary_concern`. `privacy_level` is written as an
    exhaustive-looking 3-value list with an explicit default (`public`,
    `friends_only`, `private`), so THAT one gets `Literal`-validated at
    the Pydantic layer (see app/schemas/social.py), same posture as
    `FocusSessionStatus`/`TrackerStatus` -- the column itself stays a
    plain String, matching this project's established "validate at the
    edge, store as String" convention.

    `title` has no dedicated parameter in the ask's own
    `create_achievement_post(user_id, type, raw_description, privacy)`
    signature -- see `social_service.create_achievement_post`'s docstring
    for the disclosed choice to have Gemini generate it alongside the
    card data, grounded in `achievement_type` + `raw_description`.
    """

    __tablename__ = "achievement_posts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    achievement_type: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    privacy_level: Mapped[str] = mapped_column(String(50), nullable=False, default="public")
    congrats_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Ask: "nullable=False, stores custom titles, themes, and AI quotes
    # to render on the shareable image/card" -- always populated at
    # creation time by `create_achievement_post`'s Gemini call, never
    # left null.
    shareable_card_data: Mapped[dict] = mapped_column(JSONB(), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<AchievementPost id={self.id} user_id={self.user_id} type={self.achievement_type!r}>"


class AchievementCongrats(Base):
    """Tracks individual congratulation actions -- one row per (post,
    congratulator) pair. This entire table is a disclosed necessary
    addition, not part of the ask's own model list.

    Why it has to exist: `congratulate_achievement`'s spec requires (a)
    a "limit 5 times a day" cap on the +1 token incentive per
    congratulator, and (b) implicitly, not letting one student spam
    `congrats_count` on the same post into the thousands by hammering
    the endpoint. `AchievementPost.congrats_count` is only ever an
    aggregate integer -- nothing in the ask's own model list records WHO
    congratulated WHAT or WHEN, and neither of those two requirements is
    checkable without that history. Same posture as `StudentAccountability`
    needing `last_active_at` to enforce "once per day" in Feature 4.

    `UniqueConstraint` on `(post_id, congratulator_id)` caps each student
    to congratulating any one given post exactly once (a natural,
    disclosed idempotency guard -- see `social_service.congratulate_achievement`
    for what happens on a repeat attempt). The daily token cap is a
    separate, orthogonal check: it counts how many *distinct posts* this
    user has congratulated across all of today's rows.
    """

    __tablename__ = "achievement_congrats"
    __table_args__ = (
        UniqueConstraint("post_id", "congratulator_id", name="uq_achievement_congrats_pair"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("achievement_posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    congratulator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<AchievementCongrats post_id={self.post_id} congratulator_id={self.congratulator_id}>"


class StudyDuel(Base):
    """A token-wagered Pomodoro challenge between two students -- fully
    specified by this step's own ask. `status` is written as an
    exhaustive 6-value list with no "e.g." -- `Literal`-validated at the
    Pydantic layer (see app/schemas/social.py), same posture as
    `AchievementPost.privacy_level` above.

    `challenger_id`/`opponent_id` are indexed here even though the ask's
    own field list doesn't say so for these two -- a disclosed,
    conventional addition (every other FK-to-`users.id` column in this
    codebase is indexed) needed for "my duels" style lookups by either
    participant to not require a full table scan.

    See `social_service.manage_duel`'s docstring for the full disclosed
    design on when `token_wager` actually moves between balances (at
    challenge/accept/complete time) and the transactional-locking posture
    around it.
    """

    __tablename__ = "study_duels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    challenger_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    opponent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    token_wager: Mapped[int] = mapped_column(Integer, nullable=False, default=10)

    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<StudyDuel id={self.id} challenger_id={self.challenger_id} opponent_id={self.opponent_id} status={self.status!r}>"
