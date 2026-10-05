"""Turning validated facts into (existing) `Opportunity` rows.

The agent writes into the SAME `opportunities` table the rest of APEX uses,
always with ``review_status = pending_review`` and ``is_verified = False``:
nothing the agent creates is visible to students until a human approves
it (app/agent/approval.py). The facts the legacy table has no column for
live in `agent_opportunity_facts`; dedup keys in `opportunity_fingerprints`.

Updates to an existing opportunity (dedup outcome UPDATE_EXISTING) are
recorded through the existing append-only change history service, field by
field, and never blank out a known value with a missing one.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.dates import parse_date
from app.agent.dedup import DedupFacts, is_specific_url, record_fingerprints
from app.agent.extraction import ExtractedOpportunity
from app.agent.normalization import normalize_url
from app.models.agent import (
    AgentOpportunityFacts,
    OpportunitySourceMessage,
    SourceMessage,
)
from app.models.enums import OpportunityReviewStatus, OpportunityType
from app.models.opportunity import Opportunity
from app.models.source import Source
from app.services.change_history_service import (
    normalize_for_comparison,
    record_change_if_meaningful,
)
from app.services.taxonomy import normalize_country


def _deadline_dt(value: str | None) -> dt.datetime | None:
    date = parse_date(value)
    if date is None:
        return None
    # End of day UTC: "deadline 15 Oct" means applications are open on the 15th.
    return dt.datetime.combine(date, dt.time(23, 59), tzinfo=dt.timezone.utc)


def choose_target_url(extraction: ExtractedOpportunity, message: SourceMessage, source: Source) -> str:
    """Most useful real link: application > official > first specific link
    in the message > the source's own URL. Never fabricated."""
    for candidate in (extraction.application_url, extraction.official_url):
        if candidate:
            return candidate
    for link in message.links or []:
        key = normalize_url(link)
        if key and is_specific_url(key):
            return link
    if message.links:
        return message.links[0]
    return source.base_url or f"apex://source-message/{message.id}"


def dedup_facts_from(extraction: ExtractedOpportunity, *, content_hash: str | None = None,
                     target_url: str | None = None) -> DedupFacts:
    return DedupFacts(
        title=extraction.title,
        organization=extraction.organization,
        opportunity_type=extraction.opportunity_type,
        deadline=extraction.deadline_date,
        application_url=extraction.application_url,
        official_url=extraction.official_url,
        target_url=target_url,
        funding=extraction.funding,
        eligibility=extraction.eligibility,
        content_hash=content_hash,
    )


def _country(extraction: ExtractedOpportunity) -> str:
    if extraction.is_online:
        return "Online"
    return (extraction.location or "Global")[:120]


async def link_message(db: AsyncSession, opportunity_id: uuid.UUID, message: SourceMessage, relation: str) -> None:
    exists = await db.get(OpportunitySourceMessage, (opportunity_id, message.id))
    if exists is None:
        db.add(OpportunitySourceMessage(opportunity_id=opportunity_id, source_message_id=message.id, relation=relation))
    message.opportunity_id = opportunity_id
    await db.flush()


async def create_opportunity(
    db: AsyncSession, *, extraction: ExtractedOpportunity, message: SourceMessage, source: Source
) -> Opportunity:
    target_url = choose_target_url(extraction, message, source)
    country = _country(extraction)
    opportunity = Opportunity(
        title=(extraction.title or "")[:500],
        description=extraction.description,
        target_url=target_url[:2048],
        source_id=source.id,
        type=OpportunityType(extraction.opportunity_type or OpportunityType.OTHER.value),
        deadline=_deadline_dt(extraction.deadline),
        eligibility_criteria=extraction.eligibility,
        funding_details=extraction.funding,
        location=(extraction.location or None) and extraction.location[:255],
        country=country,
        is_local_egypt=normalize_country(country) == "EG",
        min_age=extraction.age_min,
        max_age=extraction.age_max,
        is_verified=False,
        ai_confidence_score=extraction.confidence,
        raw_extracted_data={"agent_extraction": extraction.model_dump(), "source_message_id": str(message.id)},
        review_status=OpportunityReviewStatus.PENDING_REVIEW,
    )
    db.add(opportunity)
    await db.flush()
    db.add(
        AgentOpportunityFacts(
            opportunity_id=opportunity.id,
            organization=extraction.organization,
            application_url=extraction.application_url,
            official_url=extraction.official_url,
            facts=extraction.model_dump(),
        )
    )
    await db.flush()
    await record_fingerprints(
        db, opportunity.id, dedup_facts_from(extraction, content_hash=message.content_hash, target_url=target_url)
    )
    await link_message(db, opportunity.id, message, "origin")
    return opportunity


_UPDATABLE = (
    # (opportunity attribute, extraction attribute, converter)
    ("deadline", "deadline", _deadline_dt),
    ("eligibility_criteria", "eligibility", None),
    ("funding_details", "funding", None),
    ("description", "description", None),
    ("min_age", "age_min", None),
    ("max_age", "age_max", None),
)


def compute_update_proposal(opportunity: Opportunity, facts_row: AgentOpportunityFacts | None,
                            extraction: ExtractedOpportunity) -> dict[str, dict[str, str | int | None]]:
    """What the newer message would change, WITHOUT writing anything.

    Live data is never modified by the AI: the proposal is stored with the
    draft and applied only when a human approves it (`apply_update_proposal`).
    Only fields the new message actually states are considered, so a known
    value is never blanked out by a missing one."""
    proposal: dict[str, dict[str, str | int | None]] = {}

    def _plain(value):
        return value.isoformat() if isinstance(value, dt.datetime) else value

    for attr, field, convert in _UPDATABLE:
        new_value = getattr(extraction, field)
        if new_value in (None, "", []):
            continue
        new_value = convert(new_value) if convert else new_value
        old_value = getattr(opportunity, attr)
        if normalize_for_comparison(_plain(old_value)) != normalize_for_comparison(_plain(new_value)):
            proposal[attr] = {"old": _plain(old_value), "new": _plain(new_value)}
    for attr in ("organization", "application_url", "official_url"):
        value = getattr(extraction, attr)
        old = getattr(facts_row, attr) if facts_row else None
        if value and value != old:
            proposal[attr] = {"old": old, "new": value}
    return proposal


async def apply_update_proposal(
    db: AsyncSession, *, opportunity: Opportunity, proposal: dict, source_message_id: uuid.UUID | None,
    source_id: uuid.UUID | None, extraction: ExtractedOpportunity | None = None,
) -> list[str]:
    """Apply a human-approved update proposal, recording every field change
    in the existing append-only change history."""
    changed: list[str] = []
    for attr, change in proposal.items():
        new_value = change.get("new")
        if attr in {name for name, _, _ in _UPDATABLE}:
            if attr == "deadline":
                new_value = _deadline_dt(new_value)
            record = await record_change_if_meaningful(
                db,
                opportunity_id=opportunity.id,
                field_name=attr,
                old_value=change.get("old"),
                new_value=change.get("new"),
                change_reason=f"APEX agent update approved by a human (source message {source_message_id})",
                source_id=source_id,
            )
            if record is not None:
                setattr(opportunity, attr, new_value)
                changed.append(attr)
    facts = await db.get(AgentOpportunityFacts, opportunity.id)
    if facts is None:
        facts = AgentOpportunityFacts(opportunity_id=opportunity.id, facts={})
        db.add(facts)
    for attr in ("organization", "application_url", "official_url"):
        if attr in proposal and proposal[attr].get("new"):
            setattr(facts, attr, proposal[attr]["new"])
            changed.append(attr)
    if extraction is not None:
        merged = dict(facts.facts or {})
        for key, value in extraction.model_dump().items():
            if value not in (None, "", []):
                merged[key] = value
        facts.facts = merged
    await db.flush()
    return changed


async def load_facts(db: AsyncSession, opportunity_id: uuid.UUID) -> ExtractedOpportunity | None:
    row = await db.get(AgentOpportunityFacts, opportunity_id)
    if row is None or not row.facts:
        return None
    return ExtractedOpportunity.model_validate(row.facts)


async def source_of_message(db: AsyncSession, message: SourceMessage) -> Source:
    source = await db.get(Source, message.source_id)
    if source is None:  # FK makes this impossible; defensive
        raise LookupError(f"source {message.source_id} not found")
    return source


async def opportunity_messages(db: AsyncSession, opportunity_id: uuid.UUID) -> list[SourceMessage]:
    rows = await db.execute(
        select(SourceMessage)
        .join(OpportunitySourceMessage, OpportunitySourceMessage.source_message_id == SourceMessage.id)
        .where(OpportunitySourceMessage.opportunity_id == opportunity_id)
    )
    return list(rows.scalars().all())
