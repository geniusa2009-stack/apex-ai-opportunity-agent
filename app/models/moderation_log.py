"""Automatic Moderation Activity Log step -- database schema.

One row per real moderation WRITE action, written automatically at the
same time as the action itself (same DB transaction/commit -- see
`app/services/moderation_log_service.py`'s own docstring for why this is
NOT a best-effort side effect the way the Gamification XP award/
Smart-Notification dispatch on `override_video_decision` are). This is
the foundation Phases 4/5 (claim system, stats, the 3 views) build on --
see this step's own ask for why getting the shape right now matters.
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AdminRole, ModerationAction, ModerationTargetType


class ModerationLogEntry(Base):
    """WHO/WHAT/TARGET/WHEN/OUTCOME, per this step's own literal Step 1
    spec.

    `id` -- app-generated UUID (`default=uuid.uuid4`), same established
    convention as `Notification.id` (app/models/notification.py) --
    this row's own id is never needed before commit the way
    `Notification`'s is (no real-time broadcast embeds it), but kept
    consistent with this codebase's own precedent anyway rather than
    introducing a second id-generation convention with no real reason
    to differ.

    `actor_id` -- the real user id who performed the action.
    `ondelete="SET NULL"`, NULLABLE -- DELIBERATELY DIFFERENT from
    `Notification.user_id`'s `ondelete="CASCADE"`: a notification only
    matters to the user it belongs to, so it's correct for it to vanish
    with that user's account. A moderation log entry is the opposite --
    it is EVIDENCE OF WHAT HAPPENED, not a personal artifact of the
    actor, and an audit trail must survive a volunteer's account being
    deleted (e.g. someone who moderated for a month and later left).
    `SET NULL` preserves the historical row (action/target/outcome/
    when, and `actor_role` below) while only losing the now-meaningless
    live FK -- exactly the trade-off an audit log needs.

    `actor_role` -- the actor's REAL `AdminRole` AT THE TIME the action
    happened, deliberately captured as its own stored value, NOT derived
    by joining `users.admin_role` at read time. This step's own ask is
    explicit about why: "capture it at time-of-action, since roles can
    change later and the historical record shouldn't drift." A live
    join would make every past log entry silently reflect a volunteer's
    CURRENT role instead of the role they actually held when they acted
    -- e.g. a Moderator's real override from before a promotion would
    misleadingly show as a Senior Moderator action after they're
    promoted. Reuses the real `admin_role` native Postgres enum type
    (`create_type=False`, migration 0038 below) -- this is genuinely the
    same closed set of values as `User.admin_role`, not a second,
    parallel enum that could drift from it.

    `action` -- see `ModerationAction`'s own docstring
    (app/models/enums.py) for the closed set and the "next admin action,
    read this" extension note.

    `target_type` + `target_id` -- together identify what the action was
    performed on. `target_id` is DELIBERATELY NOT a foreign key: this
    column is genuinely polymorphic (a `StudentVideo.id` for the three
    video actions, a `User.id` for `CHAT_UNBAN`), and Postgres has no
    native "FK to one of several possible tables" construct -- the two
    real options were a single un-constrained UUID column plus a
    discriminator (chosen here), or two separate nullable FK columns
    (`target_video_id`/`target_user_id`) with exactly one populated per
    row. The single-column form was chosen because it matches this
    step's own literal "what (target)" singular-field wording, keeps
    every future new action (which might target a THIRD kind of row
    later) from requiring its own new nullable column, and because an
    audit log's own append-only, rarely-joined-against nature means the
    integrity guarantee a real FK gives up here (the target row could
    theoretically be deleted without the log entry knowing) matters far
    less than it would for an operational table -- the log entry's job
    is to record what was true at the time, not to stay eagerly in sync
    with the target's current existence.

    `outcome` -- nullable-`False` `JSONB`, per this step's own literal
    "outcome (what actually happened -- e.g. the override's new_status,
    confirmation the unban succeeded)" wording. Deliberately unstructured
    (not one column per possible outcome shape) -- the four real actions
    have genuinely different outcome shapes (`{"new_status": "approved"}`
    for the override/reject-and-purge actions vs. `{"was_banned": true}`
    for unban vs. `{"video_deleted": true}` for the permanent delete,
    which has no other DB-column outcome to report), matching this
    codebase's own established `Notification.payload`/
    `StudentVideo.ai_feedback` precedent for "this needs to hold
    different shapes depending on which row it's attached to."

    `created_at` -- `server_default=func.now()`, same established
    convention as every timestamp column in this codebase. No
    `updated_at` -- a log entry is immutable by construction; nothing in
    this codebase ever mutates one after insert.
    """

    __tablename__ = "moderation_log_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    actor_role: Mapped[AdminRole] = mapped_column(
        SAEnum(
            AdminRole,
            name="admin_role",
            native_enum=True,
            create_type=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )

    action: Mapped[ModerationAction] = mapped_column(
        SAEnum(
            ModerationAction,
            name="moderation_action",
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )

    target_type: Mapped[ModerationTargetType] = mapped_column(
        SAEnum(
            ModerationTargetType,
            name="moderation_target_type",
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    outcome: Mapped[dict] = mapped_column(JSONB(), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Two disclosed indexes, matching `Notification`'s own "index for
    # each real query shape" precedent:
    # - `(actor_id, created_at)` -- `GET /admin/activity-log/{user_id}`'s
    #   own "this volunteer's timeline, newest first" query.
    # - `(created_at)` alone -- `GET /admin/activity-log`'s own
    #   whole-team, date-range-filtered query, which has no `actor_id`
    #   equality filter to lead with.
    __table_args__ = (
        Index("ix_moderation_log_actor_id_created_at", "actor_id", "created_at"),
        Index("ix_moderation_log_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ModerationLogEntry id={self.id} actor_id={self.actor_id} "
            f"action={self.action} target_id={self.target_id}>"
        )
