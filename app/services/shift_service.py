"""Volunteer Coverage / Shift Schedule -- service layer.

CRUD over `ShiftAssignment` plus a real RFC 5545 `.ics` export, reusing
`deadline_alerts.py`'s own established escape/fold pattern (see
`app/models/shift.py`'s own module docstring for why these ~20 lines
are duplicated rather than imported from that module).
"""

import datetime as dt
import uuid
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.shift import ShiftAssignment
from app.models.user import User


async def find_overlapping_shift_ids(
    db: AsyncSession, *, volunteer_id: uuid.UUID, start_at: dt.datetime, end_at: dt.datetime, exclude_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """Real overlap check, scoped to the SAME volunteer only (per
    `app/models/shift.py`'s own design docstring: two DIFFERENT
    volunteers covering the same window is real, intentional double
    coverage, not something to even flag). Standard half-open interval
    overlap test: two ranges `[a, b)` and `[c, d)` overlap iff
    `a < d AND c < b`. `exclude_id` lets `update_shift` check a shift's
    own NEW window without it always "overlapping itself"."""
    query = select(ShiftAssignment.id).where(
        ShiftAssignment.volunteer_id == volunteer_id,
        ShiftAssignment.start_at < end_at,
        ShiftAssignment.end_at > start_at,
    )
    if exclude_id is not None:
        query = query.where(ShiftAssignment.id != exclude_id)
    result = await db.execute(query)
    return [row[0] for row in result.all()]


async def create_shift(
    db: AsyncSession, *, volunteer_id: uuid.UUID, start_at: dt.datetime, end_at: dt.datetime, notes: str | None, created_by: uuid.UUID
) -> tuple[ShiftAssignment, list[uuid.UUID]]:
    overlapping_ids = await find_overlapping_shift_ids(db, volunteer_id=volunteer_id, start_at=start_at, end_at=end_at)

    shift = ShiftAssignment(
        volunteer_id=volunteer_id, start_at=start_at, end_at=end_at, notes=notes, created_by=created_by
    )
    db.add(shift)
    await db.commit()
    await db.refresh(shift)
    return shift, overlapping_ids


async def update_shift(
    db: AsyncSession,
    *,
    shift: ShiftAssignment,
    volunteer_id: uuid.UUID | None,
    start_at: dt.datetime | None,
    end_at: dt.datetime | None,
    notes: str | None,
    notes_provided: bool,
) -> tuple[ShiftAssignment, list[uuid.UUID]]:
    """Applies only the real fields the caller actually sent.
    `notes_provided` distinguishes "the caller sent notes=null to
    really clear it" from "the caller never mentioned notes at all" --
    `ShiftAssignmentUpdate`'s other three fields don't need this same
    treatment since `None` is never a valid real value for any of them
    (a shift can't have no volunteer/start/end)."""
    new_volunteer_id = volunteer_id if volunteer_id is not None else shift.volunteer_id
    new_start_at = start_at if start_at is not None else shift.start_at
    new_end_at = end_at if end_at is not None else shift.end_at

    if new_end_at <= new_start_at:
        raise ValueError("end_at must be after start_at")

    overlapping_ids = await find_overlapping_shift_ids(
        db, volunteer_id=new_volunteer_id, start_at=new_start_at, end_at=new_end_at, exclude_id=shift.id
    )

    shift.volunteer_id = new_volunteer_id
    shift.start_at = new_start_at
    shift.end_at = new_end_at
    if notes_provided:
        shift.notes = notes

    db.add(shift)
    await db.commit()
    await db.refresh(shift)
    return shift, overlapping_ids


async def delete_shift(db: AsyncSession, *, shift: ShiftAssignment) -> None:
    await db.delete(shift)
    await db.commit()


async def list_shifts(db: AsyncSession, *, include_past: bool, volunteer_id: uuid.UUID | None = None) -> list[ShiftAssignment]:
    """Real, current+upcoming schedule by default (`include_past=False`
    drops any shift whose `end_at` has already passed) -- matches this
    feature's own "current schedule" framing; `include_past=True` gives
    the Owner the real full history when they actually want it (e.g.
    reviewing past coverage), same opt-in-for-history posture
    `GET /admin/activity-log`'s own date-range params already use
    elsewhere in this codebase."""
    # No relationship() on ShiftAssignment -- matches this codebase's own
    # established precedent for join/record tables not primarily
    # navigated FROM their parent (see `app/models/features.py`'s own
    # module docstring: `UserEnrollment`/`LessonProgress` don't declare
    # one either). Callers needing volunteer names batch-fetch `User`
    # rows separately -- see `admin_shifts.py`'s own `_load_volunteers`.
    query = select(ShiftAssignment).order_by(ShiftAssignment.start_at.asc())
    if not include_past:
        query = query.where(ShiftAssignment.end_at >= dt.datetime.now(timezone.utc))
    if volunteer_id is not None:
        query = query.where(ShiftAssignment.volunteer_id == volunteer_id)
    result = await db.execute(query)
    return list(result.scalars().all())


# --- iCalendar (.ics) export --------------------------------------------
# Mirrors `app/services/deadline_alerts.py`'s own `export_to_ics` RFC
# 5545 discipline exactly (manual template strings, no `icalendar`
# dependency, same fold/escape helpers) -- the one real, deliberate
# difference: a shift emits BOTH `DTSTART` and `DTEND` (a deadline
# alert is a single point-in-time milestone and only ever emits
# `DTSTART`). See `app/models/shift.py`'s own module docstring for why
# these helpers are duplicated here rather than imported.

_ICS_DATETIME_FORMAT = "%Y%m%dT%H%M%SZ"


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold_line(line: str) -> str:
    if len(line.encode("utf-8")) <= 75:
        return line

    folded_parts: list[bytes] = []
    current = b""
    for char in line:
        char_bytes = char.encode("utf-8")
        if len(current) + len(char_bytes) > 75:
            folded_parts.append(current)
            current = b" " + char_bytes
        else:
            current += char_bytes
    folded_parts.append(current)
    return "\r\n".join(part.decode("utf-8") for part in folded_parts)


def export_to_ics(shifts: list[ShiftAssignment], volunteers_by_id: dict[uuid.UUID, User]) -> str:
    """One VEVENT per real shift. SUMMARY names the real volunteer
    (falls back to their email, then to a generic label, if
    `full_name` is unset or the real `User` row wasn't found in
    `volunteers_by_id` -- same real-data-may-be-incomplete posture
    `deadline_alerts.py` doesn't need but this feature's own
    `User.full_name` nullability requires); DESCRIPTION is the real
    `notes` if any were left, omitted entirely otherwise (an empty
    DESCRIPTION line would be real, but useless, noise)."""
    now_stamp = dt.datetime.now(timezone.utc).strftime(_ICS_DATETIME_FORMAT)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Apex//Volunteer Shift Schedule//EN",
        "CALSCALE:GREGORIAN",
    ]

    for shift in shifts:
        start = shift.start_at if shift.start_at.tzinfo is not None else shift.start_at.replace(tzinfo=timezone.utc)
        end = shift.end_at if shift.end_at.tzinfo is not None else shift.end_at.replace(tzinfo=timezone.utc)
        dtstart = start.astimezone(timezone.utc).strftime(_ICS_DATETIME_FORMAT)
        dtend = end.astimezone(timezone.utc).strftime(_ICS_DATETIME_FORMAT)

        volunteer = volunteers_by_id.get(shift.volunteer_id)
        volunteer_label = (volunteer.full_name or volunteer.email or "Volunteer") if volunteer else "Volunteer"
        summary = f"APEX moderation coverage — {volunteer_label}"

        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{shift.id}@apex-platform")
        lines.append(f"DTSTAMP:{now_stamp}")
        lines.append(f"DTSTART:{dtstart}")
        lines.append(f"DTEND:{dtend}")
        lines.append(_fold_line(f"SUMMARY:{_ics_escape(summary)}"))
        if shift.notes:
            lines.append(_fold_line(f"DESCRIPTION:{_ics_escape(shift.notes)}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    return "\r\n".join(lines) + "\r\n"
