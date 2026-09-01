"""Review of Guild Content -- report-submission service layer.

Owns the one real write path a guild content report ever goes through
(`create_report`), same 3-layer posture as `chat_report_service.py`
(kept out of the router, service mutates/adds, router commits).

ANTI-MASS-REPORT: identical reasoning/enforcement to
`chat_report_service.py` -- `UNIQUE(report_id, reporter_id)` (app-layer
proactive check + a real DB constraint) makes report-count inflation by
a single actor structurally impossible. No minimum-report-count
threshold before a case surfaces in the admin queue, same "a human
spending real, identified effort to flag something deserves a human
glance, not to sit behind an arbitrary N" reasoning.

SELF-REPORT GUARD: a user cannot report their own post/comment -- same
necessary-addition posture as `chat_report_service.SelfReportError`.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ChatReportReason
from app.models.guild_moderation import REPORT_STATUS_OPEN, GuildContentReport, GuildContentReportEntry


class SelfReportError(Exception):
    """Raised when a user tries to report their own post/comment."""


class DuplicateReportError(Exception):
    """Raised when the caller has already reported this item's
    currently open case."""


async def get_open_case_for_target(
    db: AsyncSession, target_type: str, target_id: uuid.UUID
) -> GuildContentReport | None:
    """The real "is there already an open case for this item" check --
    mirrors `chat_report_service.get_open_case_for_message`."""
    result = await db.execute(
        select(GuildContentReport).where(
            GuildContentReport.target_type == target_type,
            GuildContentReport.target_id == target_id,
            GuildContentReport.status == REPORT_STATUS_OPEN,
        )
    )
    return result.scalar_one_or_none()


async def create_report(
    db: AsyncSession,
    *,
    target_type: str,
    target_id: uuid.UUID,
    author_id: uuid.UUID,
    reporter_id: uuid.UUID,
    reason: ChatReportReason,
    details: str | None,
) -> tuple[GuildContentReport, GuildContentReportEntry, int]:
    """Get-or-creates the open case for `(target_type, target_id)`, then
    adds this reporter's own entry to it. Returns
    `(case, entry, total_reports)`. Mirrors
    `chat_report_service.create_report` closely, including its
    "does NOT call db.commit(), DOES call db.flush() after a freshly
    created case" posture.
    """
    if author_id == reporter_id:
        raise SelfReportError("You cannot report your own content.")

    case = await get_open_case_for_target(db, target_type, target_id)
    if case is None:
        case = GuildContentReport(target_type=target_type, target_id=target_id)
        db.add(case)
        await db.flush()

    existing = await db.execute(
        select(GuildContentReportEntry).where(
            GuildContentReportEntry.report_id == case.id,
            GuildContentReportEntry.reporter_id == reporter_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise DuplicateReportError("You have already reported this content.")

    entry = GuildContentReportEntry(
        report_id=case.id, reporter_id=reporter_id, reason=reason, details=details
    )
    db.add(entry)
    await db.flush()

    total = (
        await db.execute(
            select(func.count())
            .select_from(GuildContentReportEntry)
            .where(GuildContentReportEntry.report_id == case.id)
        )
    ).scalar_one()

    return case, entry, total
