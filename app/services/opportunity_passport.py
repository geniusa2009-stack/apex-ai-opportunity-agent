"""Personalized Opportunity Intelligence + Opportunity Passport step,
Part E/F: a read-only structured projection over the real, existing
`Opportunity` + `Evidence`/`Claim` (D6) + `Contradiction` (D7) +
temporal (`lifecycle_state`) + `OpportunityChangeHistory` +
`EntityRelationship` (D2) data.

NOT A SECOND OPPORTUNITY MODEL (Part E's own explicit rule): nothing
here is persisted. `build_passport` reads already-persisted rows and
assembles a plain dataclass; there is no `OpportunityPassport` table,
no new column anywhere. Every field is derivable from data this
codebase already has.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contradiction import Contradiction
from app.models.enums import SourceAuthority, VerificationDecisionStatus
from app.models.evidence import Evidence
from app.models.opportunity import Opportunity
from app.models.opportunity_change_history import OpportunityChangeHistory
from app.models.user import User
from app.services import eligibility_engine
from app.services.evidence_verification import evaluate_verification_readonly

_AUTHORITATIVE_TIERS = frozenset(
    {
        SourceAuthority.OFFICIAL,
        SourceAuthority.GOVERNMENT,
        SourceAuthority.UNIVERSITY,
        SourceAuthority.KNOWN_INSTITUTION,
        SourceAuthority.TRUSTED_THIRD_PARTY,
    }
)

# Part F's own literal statuses ("VERIFIED / HOLD / NOT_VERIFIED") --
# a direct, disclosed re-mapping of the real, unmodified D6
# `VerificationDecisionStatus` (VERIFIED/HOLD/STALE/UNVERIFIED) rather
# than a new parallel status concept: STALE is folded into NOT_VERIFIED
# here (both mean "not currently trustworthy," and this Passport-level
# summary intentionally does not need STALE's finer distinction --
# `trust.verification_reason` still carries the real D6 status string
# for anyone who needs it).
_TRUST_LABELS = {
    VerificationDecisionStatus.VERIFIED: "VERIFIED",
    VerificationDecisionStatus.HOLD: "HOLD",
    VerificationDecisionStatus.STALE: "NOT_VERIFIED",
    VerificationDecisionStatus.UNVERIFIED: "NOT_VERIFIED",
}


@dataclasses.dataclass
class PassportIdentity:
    id: uuid.UUID
    title: str
    provider_source_id: uuid.UUID
    canonical_url: str
    opportunity_type: str
    raw_opportunity_type_label: str | None


@dataclasses.dataclass
class PassportEligibility:
    status: str
    matched_rules: list[str]
    failed_rules: list[str]
    unknown_rules: list[str]
    missing_information: list[str]


@dataclasses.dataclass
class PassportFunding:
    funding_details: str | None


@dataclasses.dataclass
class PassportTemporal:
    lifecycle_state: str
    lifecycle_state_reason: str | None
    deadline: dt.datetime | None
    last_verified: dt.datetime | None
    last_updated: dt.datetime
    recent_changes: list[dict]


@dataclasses.dataclass
class PassportTrust:
    verification_status: str
    verification_reason: str
    evidence_count: int
    source_count: int
    authoritative_source_count: int
    contradiction_count: int
    last_evidence_update: dt.datetime | None


@dataclasses.dataclass
class PassportProvenance:
    source_ids: list[uuid.UUID]
    evidence_ids: list[uuid.UUID]
    claim_count: int


@dataclasses.dataclass
class OpportunityPassport:
    identity: PassportIdentity
    eligibility: PassportEligibility | None
    funding: PassportFunding
    temporal: PassportTemporal
    trust: PassportTrust
    provenance: PassportProvenance


async def _load_trust(db: AsyncSession, opportunity_id: uuid.UUID, now: dt.datetime) -> tuple[PassportTrust, PassportProvenance]:
    decision = await evaluate_verification_readonly(db, opportunity_id=opportunity_id, now=now)

    evidence_result = await db.execute(select(Evidence).where(Evidence.opportunity_id == opportunity_id))
    evidence_rows = list(evidence_result.scalars().all())

    source_ids = sorted({e.source_id for e in evidence_rows if e.source_id is not None}, key=str)
    authoritative_count = sum(
        1 for e in evidence_rows if SourceAuthority(e.source_authority) in _AUTHORITATIVE_TIERS
    )
    last_evidence_update = max((e.retrieved_at for e in evidence_rows), default=None)

    contradiction_count = (
        await db.execute(
            select(func.count()).select_from(Contradiction).where(Contradiction.opportunity_id == opportunity_id)
        )
    ).scalar_one()

    trust = PassportTrust(
        verification_status=_TRUST_LABELS[decision.status],
        verification_reason=decision.reason,
        evidence_count=len(evidence_rows),
        source_count=len(source_ids),
        authoritative_source_count=authoritative_count,
        contradiction_count=contradiction_count,
        last_evidence_update=last_evidence_update,
    )
    provenance = PassportProvenance(
        source_ids=list(source_ids),
        evidence_ids=[e.id for e in evidence_rows],
        claim_count=len(decision.evaluated_claims),
    )
    return trust, provenance


async def _load_recent_changes(db: AsyncSession, opportunity_id: uuid.UUID, limit: int = 5) -> list[dict]:
    result = await db.execute(
        select(OpportunityChangeHistory)
        .where(OpportunityChangeHistory.opportunity_id == opportunity_id)
        .order_by(OpportunityChangeHistory.changed_at.desc())
        .limit(limit)
    )
    return [
        {
            "field_name": row.field_name,
            "old_value": row.old_value,
            "new_value": row.new_value,
            "changed_at": row.changed_at,
            "change_reason": row.change_reason,
        }
        for row in result.scalars().all()
    ]


async def build_passport(
    db: AsyncSession,
    *,
    opportunity: Opportunity,
    requesting_user: User | None,
    now: dt.datetime | None = None,
) -> OpportunityPassport:
    """Assembles the read-only Passport projection. `requesting_user`
    is OPTIONAL and, when provided, produces a real, personalized
    `PassportEligibility` (Part O's own "include personalized
    eligibility only when the requesting user is authorized" rule --
    "authorized" here means "a real authenticated user," matching this
    codebase's own existing `CurrentUserOptional` posture on every other
    public browsing endpoint; there is no separate per-opportunity ACL
    anywhere in this codebase to check beyond that). `None` yields
    `eligibility=None` -- never a fabricated "eligible" default for an
    anonymous caller.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    trust, provenance = await _load_trust(db, opportunity.id, now)
    recent_changes = await _load_recent_changes(db, opportunity.id)

    eligibility_out = None
    if requesting_user is not None:
        result = eligibility_engine.evaluate_eligibility(requesting_user, opportunity)
        eligibility_out = PassportEligibility(
            status=result.status.value,
            matched_rules=[r.explanation for r in result.matched_rules],
            failed_rules=[r.explanation for r in result.failed_rules],
            unknown_rules=[r.explanation for r in result.unknown_rules],
            missing_information=result.missing_information,
        )

    return OpportunityPassport(
        identity=PassportIdentity(
            id=opportunity.id,
            title=opportunity.title,
            provider_source_id=opportunity.source_id,
            canonical_url=opportunity.target_url,
            opportunity_type=opportunity.type.value,
            raw_opportunity_type_label=(opportunity.raw_extracted_data or {}).get("raw_opportunity_type_label"),
        ),
        eligibility=eligibility_out,
        funding=PassportFunding(funding_details=opportunity.funding_details),
        temporal=PassportTemporal(
            lifecycle_state=opportunity.lifecycle_state,
            lifecycle_state_reason=opportunity.lifecycle_state_reason,
            deadline=opportunity.deadline,
            last_verified=trust.last_evidence_update,
            last_updated=opportunity.updated_at,
            recent_changes=recent_changes,
        ),
        trust=trust,
        provenance=provenance,
    )
