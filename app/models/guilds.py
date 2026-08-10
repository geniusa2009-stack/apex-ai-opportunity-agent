import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Guild(Base):
    """A "Study Guild" -- a topic-anchored student community
    (Ecosystem Feature 6: AI Knowledge Realms, Anonymous Avatars & Guild
    Quests).

    IMPORTANT DISCLOSURE, same posture as `app/models/focus_room.py` from
    the prior step: this step's ask was framed as an *upgrade* to a
    pre-existing "Study Guilds" feature -- `GuildPost`/`GuildComment` are
    both described as "keep existing fields, and add," as though a full
    guilds system already existed. A repo search (Glob for `*guild*`,
    Grep for `Guild|guild` across the whole codebase and README) turned
    up zero matches -- no such feature was ever actually built here.

    Rather than re-running `AskUserQuestion` for what is structurally the
    exact same situation already resolved once this session (the missing
    "AI Pomodoro & Focus Rooms" baseline, two steps ago), this step
    applies that same, already-approved resolution directly: reconstruct
    a sensible baseline yourself, disclose it clearly, then build the
    literal upgrade on top. See the README's "Ecosystem Feature 6"
    section for the full disclosure.

    This `Guild` model itself is entirely invented baseline -- the ask
    never describes it (it only ever references `guild_id` and "the
    guild's topic" in passing), but posts/comments/quests all need
    somewhere to attach to, so a minimal community container was
    necessary. Deliberately minimal: no membership/roster table, no
    guild-level moderation roles -- any authenticated user can post into
    any guild by ID. A real membership system is a reasonable follow-on,
    not built here (see the README's "Not included" list).
    """

    __tablename__ = "guilds"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Distinct from `name` -- e.g. name="IELTS Prep Squad", topic="IELTS
    # exam preparation". Kept separate from `name` specifically because
    # this step's `generate_guild_quest` needs clean grounding text to
    # hand to Gemini ("tailored to the guild's topic," per the ask), and
    # a display name alone (which can be stylized/cute) is a noisier
    # anchor for that than an explicit topic description.
    topic: Mapped[str] = mapped_column(String(255), nullable=False)

    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Guild id={self.id} name={self.name!r}>"


class GuildPost(Base):
    """A post inside a `Guild`. `user_id` always records the REAL author
    internally (never truly anonymous to this system, only to other
    students in API responses) -- moderation and accountability require
    knowing who actually wrote something even when `is_anonymous=True`
    hides that identity from other users. See
    `app/schemas/guilds.py`/`app/api/v1/endpoints/guilds.py` for where
    that masking actually happens on the way out.
    """

    __tablename__ = "guild_posts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    guild_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("guilds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # --- BASELINE (this project's own reconstruction -- see the module
    # docstring above) ---
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- UPGRADE (this step's literal ask, "keep existing fields, and
    # add:") ---
    anonymous_alias: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<GuildPost id={self.id} guild_id={self.guild_id} is_anonymous={self.is_anonymous}>"


class GuildComment(Base):
    """A comment/review on a `GuildPost`. `is_constructive` is a BASELINE
    field (this project's own reconstruction) -- the upgrade ask
    references it directly ("mark `is_constructive` as `True`") as though
    it already existed, so it had to be part of the reconstructed
    baseline for that instruction to make sense. Nullable: `None` means
    "no rewrite was ever submitted to judge" (plain comments never get an
    is_constructive verdict), `False` means a rewrite was submitted and
    judged not (yet) constructive, `True` means it was.
    """

    __tablename__ = "guild_comments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("guild_posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # --- BASELINE ---
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_constructive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- UPGRADE (this step's literal ask, "keep existing fields, and
    # add:") ---
    target_text_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_rewrite: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<GuildComment id={self.id} post_id={self.post_id} is_constructive={self.is_constructive}>"


class GuildQuest(Base):
    """A "Guild Quest" (bounty) -- fully specified by this step's own ask
    (unlike `Guild`/`GuildPost`/`GuildComment` above, nothing here is
    invented baseline). `description` is `String`, not `Text`, per the
    ask's own literal column type -- given a generous length (1000) since
    a quest description is a short paragraph, not a single line, but the
    ask's explicit `String` typing (distinct from how `notes`/
    `description` columns elsewhere in this codebase are typed `Text`) is
    followed literally rather than silently upgraded.
    """

    __tablename__ = "guild_quests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    guild_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("guilds.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    token_reward: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<GuildQuest id={self.id} guild_id={self.guild_id} title={self.title!r}>"
