"""General Content Blacklist / Pattern Auto-Trigger addition.

STEP 1 INVESTIGATION, DISCLOSED HERE: no blacklist/pattern-list storage
existed anywhere in this codebase before this task (grepped the whole
`app/` tree for "blacklist"/"banned_word"/"profanity" -- the only hit
was `high_risk_service.py`'s own disclosure that this feature was still
unbuilt). The one existing precedent for automated content filtering is
`chat_security.contains_arabic` -- a single hardcoded regex, not a
team-manageable list, and its trigger response (a real 30-minute Redis
ban) is calibrated to one specific, hard platform rule (English-only
chat), not to "this specific piece of text matched a word we don't
allow" in general. This feature is deliberately NOT modeled on that
ban-based response -- see `blacklist_service.check_content`'s own
module docstring for the full reasoning.

`BlacklistedPattern` itself is the team-manageable list this task's own
Step 2 requires: a real DB table (Owner-CRUD via
`app/api/v1/endpoints/admin_blacklist.py`), not a hardcoded Python list
that would need a code deploy to update.
"""

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Real, named bound on pattern length -- kept small deliberately: this is
# a word/short-phrase blacklist (matching `chat_security.py`'s own single-
# character-class-sized precedent), not a place to paste in paragraphs of
# banned text. 200 chars is generous for even a multi-word phrase while
# still ruling out someone accidentally (or maliciously) pasting an
# entire essay as a single "pattern."
PATTERN_MAX_LENGTH = 200


class BlacklistedPattern(Base):
    """One real, Owner-managed blacklist entry.

    `pattern` -- the literal text (or, when `is_regex=True`, the regex
    source) to match against submitted content. Stored as-typed by the
    Owner; matching is always case-insensitive (see
    `blacklist_service.check_content`'s own docstring for why case-
    sensitivity was deliberately not made a second per-row toggle -- one
    fewer knob for a small team to have to think about, and there's no
    real scenario on this platform where a blacklisted word should be
    allowed through purely by changing its capitalization).

    `is_regex` -- Step 2's own deliberate design call: the DEFAULT
    matching mode (`is_regex=False`) is a plain, case-insensitive,
    WORD-BOUNDARY match (`\\bpattern\\b`) -- simple, predictable, and
    immune to the classic false-positive problem a naive substring match
    would have (blacklisting "ass" silently catching "class"/"assist"/
    "assignment"). This is the honest, maintainable default for a small
    team managing a short word list by hand. `is_regex=True` is an
    explicit escape hatch for the rarer case where an Owner genuinely
    needs pattern flexibility (e.g. catching a slur with common leetspeak
    substitutions) -- opt-in per row, not the default, so a team member
    adding an ordinary word never has to think about regex syntax at all.

    `description` -- optional, why this pattern exists (e.g. "common
    slur, see incident #4") -- for the Owner's own future reference, not
    shown to end users under any circumstance.

    `created_by`/`created_at` -- real audit trail for who added each
    entry and when (an Owner-editable platform policy list is exactly
    the kind of thing a team wants to be able to answer "who added this
    and why" about later). `ondelete="SET NULL"` -- same posture as
    `ModerationLogEntry.actor_id`: a departed Owner's past entries must
    keep working (and keep existing), not vanish or block deletion of
    their account.
    """

    __tablename__ = "blacklisted_patterns"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pattern: Mapped[str] = mapped_column(String(PATTERN_MAX_LENGTH), nullable=False)
    is_regex: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
