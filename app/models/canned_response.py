"""Canned Responses / Templates -- database schema.

STEP 1 INVESTIGATION: a full-repo grep (`canned`, `template`, `snippet`,
case-insensitive) turned up no existing reusable-response mechanism
anywhere in this codebase. Every action a Moderator-or-higher takes
that carries a human-written explanation -- rejecting an opportunity
(`admin_opportunities.py`), dismissing/resolving a chat or guild
report (`admin_chat_reports.py`/`admin_guild_reports.py`), resolving
an appeal (`moderation_appeals.py`), overriding/rejecting a video
(`admin_moderation.py`) -- accepts a single freeform `notes: str | None`
field (max 2000 chars) with nothing pre-fillable behind it: a genuine
gap, since these six action sites share a real, recurring set of
common explanations ("does not meet eligibility requirements",
"duplicate report, already actioned", "insufficient evidence to act")
that every volunteer currently re-types from scratch every time.

STEP 2 DESIGN, OWNER-MANAGEABLE: per this task's own literal ask, only
an Owner may create/edit/delete templates (the shared, curated set of
"what does this team consider a good explanation" wording is a real
platform-policy decision, the same class of decision this codebase
already reserves for Owner -- see `admin_blacklist.py`'s own "editing
is Owner-only, using/viewing is broader" precedent, reused here
unchanged: any Moderator-or-higher may LIST and USE templates, only an
Owner may author them).

STEP 2 DESIGN, `applies_to`: nullable `ModerationTargetType` (the SAME
enum `ModerationEscalation.item_type`/`ModerationLogEntry.target_type`
already reuse, no new enum type) -- `NULL` means "general-purpose,
shown for every item type," a real, non-null value scopes a template to
one specific queue (e.g. a `STUDENT_VIDEO`-only template about video
content standards would be actively confusing surfaced while dismissing
a chat report). Optional, not required, because plenty of real
explanations ("insufficient evidence", "handled via another channel")
are genuinely generic across every queue.

STEP 3.3, INTEGRATION DESIGN (see `app/api/v1/endpoints/
admin_canned_responses.py`'s own module docstring for the full
reasoning): rather than threading a new optional field through six
separate existing action endpoints/schemas across five different
files, integration is a single new `POST .../use` endpoint a caller
hits AT SELECTION time -- it returns the template's real `body` text
(what the caller then submits, verbatim or edited, as that action's own
EXISTING `notes` field) and increments `times_used` server-side. This
is the real, minimal-footprint integration point: the six existing
action flows need zero changes to their own request schemas, and
`times_used` becomes real, honest usage data (not a guess), because it
is only ever incremented by an actual real use, not by merely viewing
the list.
"""

import datetime as dt
import uuid

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ModerationTargetType
from app.models.mixins import TimestampMixin

CANNED_RESPONSE_TITLE_MAX_LENGTH = 200
CANNED_RESPONSE_BODY_MAX_LENGTH = 2000  # matches every real `notes` field's own cap it's meant to fill


class CannedResponse(Base, TimestampMixin):
    """One reusable response template. See this module's own docstring
    for the full Step 1/2/3.3 investigation and design.

    `title` -- short, internal label shown in a picker list (NOT sent
    anywhere user-facing itself).

    `body` -- the real reusable text, same max length as every existing
    `notes` field it's meant to fill (see module docstring).

    `applies_to` -- nullable, reuses `ModerationTargetType` unchanged.

    `created_by` -- `ondelete="SET NULL"`, same "a shared, team-curated
    resource must survive the authoring Owner's account being deleted
    later" reasoning `BlacklistedPattern.created_by` already
    established.

    `times_used` -- real, server-incremented usage count (see module
    docstring's Step 3.3 reasoning for why this is only ever bumped by
    an actual `POST .../use` call, never by listing).
    """

    __tablename__ = "canned_responses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    applies_to: Mapped[ModerationTargetType | None] = mapped_column(
        SAEnum(
            ModerationTargetType,
            name="moderation_target_type",
            native_enum=True,
            create_type=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    times_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<CannedResponse id={self.id} title={self.title!r} applies_to={self.applies_to}>"
