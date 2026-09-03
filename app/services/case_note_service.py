"""Internal Notes on Cases -- service layer."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.case_note import CaseNote
from app.models.enums import ModerationTargetType


class CaseNoteNotFoundError(Exception):
    pass


async def add_note(
    db: AsyncSession, *, item_type: ModerationTargetType, item_id: uuid.UUID, body: str, author_id: uuid.UUID
) -> CaseNote:
    note = CaseNote(item_type=item_type, item_id=item_id, body=body, author_id=author_id)
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


async def list_notes(
    db: AsyncSession, item_type: ModerationTargetType, item_id: uuid.UUID
) -> tuple[list[CaseNote], int]:
    """Oldest first -- a real conversation thread reads top-to-bottom in
    the order it was actually written, same convention this codebase's
    other real listing endpoints use when the item genuinely IS an
    ordered sequence of events (unlike a review QUEUE, which surfaces
    the longest-WAITING item first for a different reason)."""
    filters = (CaseNote.item_type == item_type, CaseNote.item_id == item_id)
    total = (await db.execute(select(func.count()).select_from(CaseNote).where(*filters))).scalar_one()
    result = await db.execute(select(CaseNote).where(*filters).order_by(CaseNote.created_at.asc()))
    return list(result.scalars().all()), total


async def get_note_or_raise(db: AsyncSession, note_id: uuid.UUID) -> CaseNote:
    note = await db.get(CaseNote, note_id)
    if note is None:
        raise CaseNoteNotFoundError(str(note_id))
    return note


async def delete_note(db: AsyncSession, note_id: uuid.UUID) -> None:
    note = await get_note_or_raise(db, note_id)
    await db.delete(note)
    await db.commit()
