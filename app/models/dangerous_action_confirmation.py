"""Two-Person Confirmation for Dangerous Actions -- database schema.

STEP 1 INVESTIGATION (this task's own required first step): the complete
real list of irreversible/destructive actions in this codebase, confirmed
by reading `AdminRole`'s own docstring (`app/models/enums.py`) and every
real `RequireSeniorModerationAccess` call site, is exactly FOUR --

- `ModerationAction.VIDEO_DELETE` (`DELETE /admin/videos/{video_id}`,
  `admin_moderation.py::delete_video_permanently`) -- row delete + real
  R2/storage deletion, no undo path.
- `ModerationAction.VIDEO_REJECT_AND_PURGE` (`POST /admin/videos/
  {video_id}/reject-and-purge-media`, `admin_moderation.py::
  reject_video_and_purge_media`) -- keeps the row, purges the underlying
  media permanently.
- `ModerationAction.CHAT_MESSAGE_DELETE` (`DELETE /admin/chat/reports/
  {report_id}/message`, `admin_chat_reports.py::delete_reported_message`)
  -- "genuinely irreversible -- there is no undo endpoint," per that
  route's own docstring.
- `ModerationAction.GUILD_CONTENT_DELETE` (`DELETE /admin/guild/reports/
  {report_id}/content`, `admin_guild_reports.py::
  delete_reported_guild_content`) -- "genuinely irreversible ... same
  bucket `DELETE /admin/chat/reports/{report_id}/message` is in," per
  that route's own docstring.

Every OTHER `RequireSeniorModerationAccess` route was checked and ruled
out: `POST /admin/moderation/{item_type}/{item_id}/escalate`'s own
resolve action, `POST /admin/appeals/{appeal_id}/resolve` (explicitly "+
real reversal" per its own docstring -- the opposite of irreversible),
and `POST /admin/opportunities` (manual creation -- gated at Senior
Moderator for a "who gets to personally hand-type a listing" access
reason, not because it is destructive; its own module docstring says so
explicitly: "nothing here is irreversibly destructive"). This confirms
the four above are the COMPLETE real list -- not three, DISCLOSED beyond
this task's own prompt wording (which named three by example and asked
this step to confirm the real, complete list): `CHAT_MESSAGE_DELETE` is
a real fourth member of the same bucket, already logged as
`ModerationAction.CHAT_MESSAGE_DELETE` since migration 0040.

STEP 2 DESIGN: a real pending-confirmation state machine, genuinely
polymorphic across all four actions above, reusing the EXACT same
`item_type`/`item_id` discriminator convention `ModerationLogEntry`/
`ModerationEscalation` already established (`app/models/
moderation_escalation.py`'s own module docstring has the fuller "why a
single UUID + enum discriminator, not per-table columns" reasoning,
which applies identically here) -- one small table, not four different
"is this pending" columns bolted onto `StudentVideo`/`ChatMessageReport`/
`GuildContentReport`.

`action` reuses the SAME `ModerationAction` native enum type unchanged
(`create_type=False`) -- restricted to exactly the four members above at
the APPLICATION layer (`dangerous_action_service._DANGEROUS_ACTIONS`),
not by a DB CHECK constraint: a future FIFTH destructive action is a
real, anticipated possibility (see that module's own "NEXT ADMIN ACTION"
note), and a hand-written Postgres CHECK naming four literal enum values
would need its own migration to widen at that point anyway -- no safer
than the application-level frozenset this codebase already uses
elsewhere for a closed, real subset of a larger enum (see
`moderation_log_service.py::_DIRECT_TARGET_BAN_ACTIONS` for the exact
same "real subset of a bigger enum, enforced in Python, not the schema"
precedent).

`status` is a plain, non-native `String(20)` column with real Python
module-level constants (`CONFIRMATION_STATUS_PENDING`/
`CONFIRMATION_STATUS_EXECUTED`) -- DELIBERATELY NOT a third native
Postgres enum type. This mirrors `GuildContentReport`/`ChatMessageReport`
own established "case/lifecycle status" convention (`REPORT_STATUS_OPEN`/
`REPORT_STATUS_DISMISSED`/`REPORT_STATUS_CONTENT_DELETED`, plain string
constants), which `AdminRole`'s own docstring explicitly contrasts with
its own "small, stable, unlikely-to-change classification enum" native-
enum posture. A confirmation's status is exactly that: a two-state case
lifecycle (pending -> executed), not a small fixed vocabulary shared and
compared across many unrelated tables the way `AdminRole`/
`ModerationAction`/`ModerationTargetType` are -- so it gets the lighter-
weight convention, avoiding a THIRD migration-only-to-add-a-value native
enum for what is really just two case-shaped strings.

Only two real states, deliberately -- NOT a third "rejected"/"denied"
state. This task's own ask is a real approve-or-override gate, not a
review workflow with a rejection path; a Senior Moderator who disagrees
with a pending request simply never approves it (it stays `pending`,
fully visible on the team-wide queue this feature's own `GET /admin/
moderation/dangerous-actions` exposes, for anyone -- including the
original requester -- to see it hasn't moved). Adding a `rejected` state
with no real behavior attached to it (nothing in this task's ask reverses
or notifies on a "no") would be exactly the kind of ad hoc extra state
this task's own governing methodology says not to invent.

`is_override` -- the OWNER-OVERRIDE DESIGN DECISION, disclosed per this
task's own explicit ask ("state this design decision clearly"): the user
raised a real, specific concern -- a hard two-person requirement could
block the Owner from taking an urgent, necessary irreversible action if
no second real person is available. Rather than a silent bypass (e.g. an
Owner's own `approve` call quietly self-approving), the override is a
SEPARATE, DELIBERATE act: a distinct real endpoint
(`POST /admin/moderation/{item_type}/{item_id}/dangerous-actions/
{action}/override`, `RequireOwner`-gated, `admin_dangerous_actions.py`),
which flips this same boolean and is logged under a THIRD, DISTINCT
`ModerationAction` member (`DANGEROUS_ACTION_OVERRIDDEN`) never reused
for a normal second-approval (`DANGEROUS_ACTION_APPROVED`) -- so the real
audit trail always shows, at a glance, whether an irreversible action
went through real two-person confirmation or was a deliberate, visible,
Owner-only emergency override. `is_override=True` also means
`requested_by` may legitimately equal `executed_by` (the Owner acting
alone) -- the self-approval rule this feature otherwise enforces
(`dangerous_action_service.approve_confirmation`) applies ONLY to the
normal `approve` path, never to `override`, by construction (they are
two different functions, not one function with a bypass flag).

STEP 3 DESIGN, ENFORCEMENT (why this actually gates the four routes,
not just adds parallel, ignorable new ones): `delete_video_permanently`/
`reject_video_and_purge_media`/`delete_reported_message`/
`delete_reported_guild_content` themselves are the real "request" step
now -- each one's own body was changed (see each file's own "Real
Two-Person Confirmation" comment at its call site) to call
`dangerous_action_service.request_confirmation(...)` and return a real
202 Accepted / `DangerousActionPendingResponse` instead of executing the
mutation directly. The actual mutation logic for all four (unchanged
from before this task, moved verbatim, not rewritten) now lives in
`dangerous_action_service.py`'s four `_execute_*` functions, the ONE
place `approve_confirmation`/`override_confirmation` call into -- so a
plain Senior Moderator hitting the ORIGINAL, familiar URL a second time
gets back the SAME still-pending confirmation (idempotent, same posture
`moderation_escalation_service.raise_escalation` already established),
never a second execution.

`requested_by`/`requested_at` -- who asked for the action and when.
`executed_by`/`executed_at` -- who actually made it happen (the second
approver, or the overriding Owner) and when, both `NULL` while
`status == CONFIRMATION_STATUS_PENDING`. Both FKs `ondelete="SET NULL"`,
same "an audit-adjacent record must survive an account being deleted
later" reasoning `ModerationEscalation.escalated_by`/`.resolved_by`
already established.

`notes` -- `Text`, nullable: the SAME optional free-text field
`AdminDeleteVideoRequest`/`AdminRejectAndPurgeRequest`/
`AdminChatReportDeleteMessageRequest`/`AdminGuildReportDeleteContentRequest`
already accept, captured at REQUEST time and carried through to the real
underlying action's own `ModerationLogEntry.outcome["notes"]` unchanged
(see `dangerous_action_service.py`'s own `_execute_*` functions -- each
one's outcome dict is byte-for-byte the same shape the original inline
handler body wrote before this task, just moved).
"""

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ModerationAction, ModerationTargetType

CONFIRMATION_STATUS_PENDING = "pending"
CONFIRMATION_STATUS_EXECUTED = "executed"


class DangerousActionConfirmation(Base):
    """One row per real "irreversible action requested" event -- see this
    module's own docstring for the full Step 1/2/3 reasoning."""

    __tablename__ = "dangerous_action_confirmations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    action: Mapped[ModerationAction] = mapped_column(
        SAEnum(
            ModerationAction,
            name="moderation_action",
            native_enum=True,
            create_type=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )

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

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=CONFIRMATION_STATUS_PENDING)
    is_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    executed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Two disclosed indexes, same "one per real query shape" precedent
    # `ModerationEscalation`/`ModerationLogEntry` already established:
    # - `(item_type, item_id, action, status)` -- "is THIS action on THIS
    #   item already pending" (`request_confirmation`'s own idempotency
    #   check, and `override_confirmation`'s own "reuse the existing
    #   pending row instead of creating a duplicate" lookup).
    # - `(status, requested_at)` -- `GET /admin/moderation/dangerous-
    #   actions`'s own whole-team "everything currently pending, oldest
    #   first" query.
    __table_args__ = (
        Index(
            "ix_dangerous_action_confirmations_item",
            "item_type", "item_id", "action", "status",
        ),
        Index("ix_dangerous_action_confirmations_status", "status", "requested_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<DangerousActionConfirmation id={self.id} action={self.action} "
            f"item_type={self.item_type} item_id={self.item_id} status={self.status}>"
        )
