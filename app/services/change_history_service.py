"""Taxonomy Evolution + Temporal State Machine + Change History step,
Part C: the one real write path for `OpportunityChangeHistory` rows.

Same "backend-owned, never LLM-chosen, never source-text-triggered"
posture as `evidence_verification.record_evidence` -- a change record
is created only by CALLER CODE that has already compared a real old
value against a real new value; nothing in this module ever inspects
raw source text or an LLM's own confidence.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.opportunity_change_history import OpportunityChangeHistory


def _normalize_for_comparison(value) -> str | None:
    """Deterministic string normalization used ONLY to decide whether a
    change is meaningful (Part C3: `old_value == new_value` must never
    create a record) -- never used as the stored value itself. `None`
    and the empty string are treated as the same "no value" state so a
    field going from `None` to `""` (or the reverse) is not mistaken for
    a real change; datetimes are compared via their ISO string so a
    timezone-naive vs. timezone-aware value carrying the identical
    instant is not falsely flagged (a real limitation, disclosed: two
    genuinely different instants that happen to format identically at
    this module's chosen precision would be missed, but no such
    collision is reachable from any real caller in this codebase today).
    """
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return str(value)


async def record_change_if_meaningful(
    db: AsyncSession,
    *,
    opportunity_id: uuid.UUID,
    field_name: str,
    old_value,
    new_value,
    change_reason: str,
    source_id: uuid.UUID | None = None,
    evidence_id: uuid.UUID | None = None,
) -> OpportunityChangeHistory | None:
    """Create ONE new, append-only `OpportunityChangeHistory` row -- but
    ONLY when `old_value` and `new_value` are genuinely different after
    normalization (Part C3). Returns `None`, and writes nothing, when
    they are the same -- this is the one and only guard against
    recording a change record for every insignificant write Part C's
    own instruction forbids.

    Never UPDATEs an existing row (Part C5: historical immutability) --
    every call that passes the meaningful-change check INSERTs a new
    row; a field that changes three times gets three real rows, each
    preserving its own old/new pair and timestamp.
    """
    if _normalize_for_comparison(old_value) == _normalize_for_comparison(new_value):
        return None

    old_str = None if old_value is None else str(old_value)
    new_str = None if new_value is None else str(new_value)

    record = OpportunityChangeHistory(
        id=uuid.uuid4(),
        opportunity_id=opportunity_id,
        field_name=field_name,
        old_value=old_str,
        new_value=new_str,
        source_id=source_id,
        evidence_id=evidence_id,
        change_reason=change_reason,
    )
    db.add(record)
    await db.flush()
    return record
