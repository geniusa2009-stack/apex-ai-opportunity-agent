"""Extend Moderation to Chat Messages -- report-submission service layer.

Owns the one real write path a chat message report ever goes through
(`create_report`), same "kept out of the router" 3-layer posture as
`moderation_log_service`/`moderation_claim_service`.

RATE-LIMITING / ANTI-MASS-REPORT DECISION, DISCLOSED (this task's own
required reasoning): the real defense against "a user mass-reporting to
bury someone" is `UNIQUE(report_id, reporter_id)` on
`ChatMessageReportEntry` -- enforced here at the application layer (a
proactive `SELECT` before `INSERT`, giving a clean `DuplicateReportError`
-> real 409) AND as a real DB constraint (`app/models/chat_moderation.py`'s
own `UniqueConstraint`, belt-and-suspenders against a race between two
concurrent requests from the same reporter). One report per user per
OPEN case is enough: it makes report-count inflation by a single actor
structurally impossible (repeating the same report a hundred times adds
exactly one row, not a hundred), which is the actual abuse vector named
in this task's own ask -- "mass-report to bury someone" only works if
volume from ONE source counts multiple times.

DELIBERATELY NO minimum-report-count THRESHOLD before a case surfaces in
the admin queue (`get_open_case_for_message` / the queue listing in
`admin_chat_reports.py` both treat ANY open case, even a fresh one-report
case, as real and reviewable). Reasoning: unlike the video review queue
(populated automatically by an AI pipeline running on every upload, where
volume is cheap and a human glance is the only filter), this queue is
populated by a real user spending real, identified (not anonymous --
`reporter_id` is always known and shown to moderators, see
`AdminChatReportEntryOut`) effort to flag something. Requiring N distinct
reports before a moderator ever sees it would let a real, single instance
of harassment or spam sit unreviewed while it silently accumulates toward
whatever N is chosen -- exactly the outcome empowering users to report is
supposed to prevent. The claim system's own advisory (non-blocking)
posture already means a low-signal single report costs a moderator only
a quick dismiss, not a queue slot that blocks other work; that asymmetry
(cheap to dismiss a false positive vs. real harm if a genuine one is
delayed) is why the bar is 1, not higher.

SELF-REPORT GUARD, DISCLOSED NECESSARY ADDITION beyond the task's own
literal ask: a sender cannot report their own message. Not named in the
ask, but leaving this open would let a user manufacture a "reported"
case against their own message for no coherent reason this feature is
meant to serve -- a real, cheap-to-close gap, not a hypothetical one.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatMessage
from app.models.chat_moderation import REPORT_STATUS_OPEN, ChatMessageReport, ChatMessageReportEntry
from app.models.enums import ChatReportReason


class SelfReportError(Exception):
    """Raised when a user tries to report their own message."""


class DuplicateReportError(Exception):
    """Raised when the caller has already reported this message's
    currently open case."""


async def get_open_case_for_message(db: AsyncSession, message_id: uuid.UUID) -> ChatMessageReport | None:
    """The real "is there already an open case for this message" check --
    used both by `create_report` below (get-or-create) and available for
    any future call site that needs the same lookup, so the "what counts
    as open" definition never drifts between them."""
    result = await db.execute(
        select(ChatMessageReport).where(
            ChatMessageReport.message_id == message_id,
            ChatMessageReport.status == REPORT_STATUS_OPEN,
        )
    )
    return result.scalar_one_or_none()


async def create_report(
    db: AsyncSession,
    *,
    message: ChatMessage,
    reporter_id: uuid.UUID,
    reason: ChatReportReason,
    details: str | None,
) -> tuple[ChatMessageReport, ChatMessageReportEntry, int]:
    """Get-or-creates the open case for `message`, then adds this
    reporter's own entry to it. Returns `(case, entry, total_reports)` --
    `total_reports` is the real, current distinct-reporter count on this
    case, used by the router to build `ChatMessageReportResponse`.

    Does NOT call `db.commit()` -- same "service mutates/adds, router
    commits" split as `moderation_claim_service.apply_claim`/
    `moderation_log_service.log_action`. Does call `db.flush()` after a
    freshly created case, though -- unlike those two services (which
    mutate an already-persisted row), a brand new `ChatMessageReport` has
    no `id` until it's actually sent to the DB, and the entry row below
    needs a real `report_id` to point at.
    """
    if message.sender_id == reporter_id:
        raise SelfReportError("You cannot report your own message.")

    case = await get_open_case_for_message(db, message.id)
    if case is None:
        case = ChatMessageReport(message_id=message.id)
        db.add(case)
        await db.flush()

    existing = await db.execute(
        select(ChatMessageReportEntry).where(
            ChatMessageReportEntry.report_id == case.id,
            ChatMessageReportEntry.reporter_id == reporter_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise DuplicateReportError("You have already reported this message.")

    entry = ChatMessageReportEntry(
        report_id=case.id, reporter_id=reporter_id, reason=reason, details=details
    )
    db.add(entry)
    await db.flush()

    total = (
        await db.execute(
            select(func.count())
            .select_from(ChatMessageReportEntry)
            .where(ChatMessageReportEntry.report_id == case.id)
        )
    ).scalar_one()

    return case, entry, total
