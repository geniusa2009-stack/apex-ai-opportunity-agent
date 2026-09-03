"""Internal Notes on Cases -- database schema.

STEP 1 INVESTIGATION: `StudentVideo.admin_notes` (app/models/
student_videos.py) is the one existing free-text field on any real
case/item type -- but it is a SINGLE scalar column, OVERWRITTEN (not
appended to) on every `override_video_decision`/
`reject_video_and_purge_media` call (see that endpoint's own disclosed
"not a list/audit table of every override this video has ever
received" scope-boundary docstring), exists on `StudentVideo` alone
(none of the other four claimable/reviewable item types have anything
similar), and is really a DECISION artifact (bundled with a status
change), not a standing place to jot context BEFORE a decision is
made. A genuine, distinct gap: nowhere can a Moderator leave a shared
note for teammates on a case they haven't decided yet, or add a SECOND
note without silently erasing a colleague's first one.

STEP 2 DESIGN: a new, small, genuinely polymorphic table, same
`item_type`/`item_id` convention `ModerationEscalation` already
established (reusing the SAME `ModerationTargetType` enum, and the
SAME `admin_claims._CLAIMABLE_MODELS` dispatch table -- see
`app/api/v1/endpoints/admin_case_notes.py`'s own module docstring).
Deliberately MANY rows per item (a real append-only thread), NOT one
scalar column per item the way `StudentVideo.admin_notes` is -- this is
the one real behavioral difference from that existing field this
feature exists to fix: multiple volunteers can each leave their own
note on the same case without clobbering each other's.

STEP 2 DESIGN, IMMUTABLE BODY: notes have no `PATCH`/edit endpoint --
once posted, a note's `body` never changes (same "an audit-adjacent
record is append-only, not silently rewritable" posture
`ModerationLogEntry` already established for actions; a note is a
lighter-weight, less formal record, but still real, shared context
other volunteers rely on, so quietly rewriting it after the fact would
be misleading). A genuine mistake (wrong item, typo, meant for a
different case) is handled by DELETE, not edit -- see
`admin_case_notes.py`'s own docstring for who may delete.

STEP 2 DESIGN, VISIBILITY: every route lives under `/admin/...`, gated
by the SAME `RequireModerationAccess` dependency every other internal-
only moderation surface already uses -- notes are, by construction,
never reachable from any student-facing endpoint. No separate
"internal-only" flag is needed on the row itself; the whole table is
internal by virtue of which router exposes it, the same posture
`ModerationLogEntry`/`ModerationEscalation` already have.
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ModerationTargetType

CASE_NOTE_BODY_MAX_LENGTH = 2000  # matches every existing `notes` field's own cap


class CaseNote(Base):
    """One real, shared internal note left on a claimable/reviewable
    item. See this module's own docstring for the full Step 1/2
    investigation and design.

    `item_type`/`item_id` -- the same polymorphic discriminator
    convention `ModerationEscalation`/`ModerationLogEntry` already
    established.

    `body` -- `Text`, `nullable=False`: an empty note carries no real
    information.

    `author_id` -- `ondelete="SET NULL"`, same established "a shared,
    team-visible record must survive the authoring volunteer's account
    being deleted later" reasoning every other actor-tracking column in
    this codebase already follows.

    `created_at` -- when it was posted; no `updated_at` (see module
    docstring's immutable-body reasoning -- nothing on this row is ever
    updated in place, only the whole row is deleted).
    """

    __tablename__ = "case_notes"

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

    body: Mapped[str] = mapped_column(Text, nullable=False)

    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # One real query shape: "every note on THIS item, oldest first" --
    # `GET /admin/moderation/{item_type}/{item_id}/notes`'s own thread
    # view.
    __table_args__ = (
        Index("ix_case_notes_item", "item_type", "item_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<CaseNote id={self.id} item_type={self.item_type} item_id={self.item_id}>"
