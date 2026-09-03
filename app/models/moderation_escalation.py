"""Escalation Path Between Volunteers -- database schema.

STEP 1 INVESTIGATION: confirmed no generic "flag this item as needing
more senior attention" mechanism exists anywhere in this codebase.
`chat_report_escalate_ban` (`admin_chat_reports.py`) is a real but
UNRELATED concept -- it is a Moderator DIRECTLY BANNING a user off the
back of a reported message (a severity upgrade a Moderator-or-higher
already has full authority to perform alone), not a request routed to
someone more senior. The Claim System's `apply_release` Owner-force-
release path (`moderation_claim_service.py`) is the closest adjacent
precedent, but it only lets an OWNER reclaim an item someone else is
sitting on -- it has no "a plain Moderator flags this FOR a senior
reviewer" direction at all, and produces no visible, filterable signal
of its own. Confirmatory precedent that this real gap was already
anticipated: the New-Volunteer Onboarding Checklist's own step
(`app/models/onboarding.py`, key `know_escalation_path`) literally
labels "know how to escalate a serious case ... to someone with a
higher tier if you don't have it yourself" as a real onboarding skill
-- with no actual technical feature behind it until now.

STEP 2 DESIGN: a NEW, small, genuinely polymorphic table -- deliberately
NOT `escalated_by`/`escalated_at` columns bolted onto each of the five
existing claimable models (`StudentVideo`/`ChatMessageReport`/
`ModerationAppeal`/`Opportunity`/`GuildContentReport`). That would mean
five separate migrations touching five different tables for one
feature, and -- unlike the Claim System, which needed per-item MUTABLE
state directly on the row it claims because `get_effective_claim` reads
it inline on every review-queue listing query -- an escalation is far
closer in shape to `ModerationLogEntry`: a real event record with a
WHO/WHAT/TARGET/WHEN, that this feature's own "show me everything
currently escalated, across every item type, so a senior volunteer can
see what needs them" view (see `admin_escalations.py`) needs to query
independently of any specific item type's own table. This table
reuses the EXACT same `target_type`/`target_id`-style polymorphic
discriminator convention `ModerationLogEntry` already established
(`item_type` here, reusing the SAME `ModerationTargetType` enum
unchanged -- no new enum type), rather than inventing a second,
parallel polymorphic pattern.

Distinct from claiming/dismissing, per this task's own ask: claiming
(`moderation_claim_service.py`) is "I am working on this" (advisory,
self-directed, expires after 30 minutes of inactivity). Escalating is
"this needs someone MORE SENIOR than me" (an explicit request FOR
another, more senior person, that does not expire and stays visible
until a Senior-Moderator-or-higher actually resolves it -- see
`app/api/v1/endpoints/admin_escalations.py`'s own tier-gating
docstring for why raising an escalation and resolving one are
DELIBERATELY gated at different tiers). An item can be simultaneously
claimed (by anyone) AND escalated (by anyone) -- the two states are
orthogonal, exactly as claiming and the review-queue's own
`review_status`/`status` are already orthogonal to each other in every
existing claimable queue.

`resolved_at IS NULL` is "this escalation is still active" -- the one
real, queried state this whole feature exists to expose (see
`admin_escalations.py::list_active_escalations`). Deliberately no
row-level uniqueness constraint enforcing "at most one active
escalation per item" -- enforced instead as an application-level
idempotency check in `moderation_escalation_service.raise_escalation`
(mirroring `moderation_claim_service.apply_claim`'s own "re-calling is
idempotent, not an error" posture, not a hard DB constraint), since a
partial unique index (`WHERE resolved_at IS NULL`) is a real, available
Postgres feature this codebase has simply never used anywhere else, and
introducing that pattern for the first time here for a low-contention,
human-paced (not high-concurrency) write path is not a proportionate
new precedent to set.
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ModerationTargetType


class ModerationEscalation(Base):
    """One row per real "flag this item for more senior attention"
    event. See this module's own docstring for the full Step 1/2
    reasoning.

    `item_type` + `item_id` -- the same polymorphic discriminator
    convention `ModerationLogEntry.target_type`/`.target_id` already
    established (see that column's own docstring for why a single
    un-constrained UUID + enum discriminator, not a real FK, is the
    right shape for a genuinely polymorphic reference). Reuses the
    SAME `ModerationTargetType` enum (no new enum type), and the SAME
    five real dispatch entries the Claim System's own
    `admin_claims.py::_CLAIMABLE_MODELS` already established (STUDENT_VIDEO/
    CHAT_MESSAGE_REPORT/MODERATION_APPEAL/OPPORTUNITY/GUILD_CONTENT_REPORT)
    -- imported and reused unchanged by `admin_escalations.py`, not
    redefined a second time.

    `note` -- `Text`, `nullable=False`: WHY this needs senior attention
    is the one piece of real information a Moderator raising an
    escalation must supply for it to be useful to whoever picks it up;
    an escalation with no reason attached would be strictly less useful
    than the review queue's own listing, which already shows the raw
    item.

    `escalated_by`/`escalated_at` -- who raised it and when.
    `ondelete="SET NULL"` on `escalated_by`, matching
    `ModerationLogEntry.actor_id`'s own established "an audit-adjacent
    record must survive the raising volunteer's account being deleted
    later" reasoning.

    `resolved_by`/`resolved_at` -- who cleared it and when, both `NULL`
    while still active. Deliberately NOT a foreign key CASCADE either
    way -- same reasoning as `escalated_by`.
    """

    __tablename__ = "moderation_escalations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    item_type: Mapped[ModerationTargetType] = mapped_column(
        SAEnum(
            ModerationTargetType,
            name="moderation_target_type",
            native_enum=True,
            create_type=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    note: Mapped[str] = mapped_column(Text, nullable=False)

    escalated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    escalated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Two disclosed indexes, matching `ModerationLogEntry`'s own "one
    # index per real query shape" precedent:
    # - `(item_type, item_id, resolved_at)` -- "is THIS item currently
    #   escalated" (raise_escalation's own idempotency check, and any
    #   future per-item-detail view).
    # - `(resolved_at)` alone -- `GET /admin/escalations`'s own
    #   whole-team "everything currently active" query, which has no
    #   `item_type`/`item_id` equality filter to lead with.
    __table_args__ = (
        Index("ix_moderation_escalations_item", "item_type", "item_id", "resolved_at"),
        Index("ix_moderation_escalations_resolved_at", "resolved_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ModerationEscalation id={self.id} item_type={self.item_type} "
            f"item_id={self.item_id} resolved={self.resolved_at is not None}>"
        )
