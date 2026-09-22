"""Opportunity Intelligence Hardening, Part 1: Canonical Opportunity
Contract.

DISCLOSED, DELIBERATE NON-DUPLICATION (Part 20's own instruction): this
is NOT a second `Opportunity` model, NOT a second persistence path, and
writes nothing to the database. `app.models.opportunity.Opportunity` is
and remains the one real, persisted row; `app.models.claim.Claim` /
`app.models.evidence.Evidence` are and remain the one real evidence
system. This module is a pure, read-only PROJECTION -- it formalizes,
in one typed place, the full shape Part 1 asks for (identity,
classification, eligibility, application, funding, temporal,
provenance) by assembling it from those already-existing rows, making
explicit which of Part 1's requested fields this codebase can actually
populate today and which are honestly `None` because no extraction path
for them exists yet (confirmed by direct model/pipeline audit -- see
this task's own report, Section 3).

UNKNOWN preservation (this module's central rule): every field is
`Optional`, and a field is left `None` rather than guessed whenever the
underlying `Opportunity`/`Claim` data doesn't actually support it. This
module performs NO inference of its own -- it only reads what is
already there.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.claim import Claim
from app.models.enums import AcademicLevel, Gender, OpportunityType
from app.models.opportunity import Opportunity


class CanonicalEligibility(BaseModel):
    """Part 1's ELIGIBILITY group. Only the sub-fields this codebase's
    real `Opportunity` columns actually carry today are populated;
    everything else (residency, field/discipline, experience
    requirements) has no real extraction path yet (confirmed via a full
    model audit) and is always `None` here -- never guessed."""

    model_config = ConfigDict(frozen=True)

    minimum_age: int | None = None
    maximum_age: int | None = None
    nationality: str | None = None  # not extracted today; always None (honest gap)
    residency: str | None = None  # not extracted today; always None (honest gap)
    academic_requirements: list[AcademicLevel] | None = None
    field_or_discipline: str | None = None  # not extracted today; always None (honest gap)
    experience_requirements: str | None = None  # not extracted today; always None (honest gap)
    gender: Gender | None = None
    raw_eligibility_text: str | None = None


class CanonicalFunding(BaseModel):
    """Part 1's FUNDING group. `funding_type_labels` is the output of
    `funding_intelligence.classify_funding_text` when the caller chooses
    to run it -- this module never runs classification itself (staying a
    pure projection), only carries whatever the caller supplies."""

    model_config = ConfigDict(frozen=True)

    raw_funding_text: str | None = None
    funding_type_labels: tuple[str, ...] = Field(default_factory=tuple)
    amount: str | None = None  # never populated unless a real explicit amount was extracted; None = UNKNOWN


class CanonicalTemporal(BaseModel):
    model_config = ConfigDict(frozen=True)

    published_at: dt.datetime | None = None
    deadline: dt.datetime | None = None
    deadline_state: str | None = None  # a `deadline_intelligence.DeadlineState` value, when classified
    status: str = "unknown"  # DISCLOSED: the Part 10 temporal state machine is not implemented this pass
    last_verified_at: dt.datetime | None = None
    source_updated_at: dt.datetime | None = None  # not extracted today; always None (honest gap)


class CanonicalProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: uuid.UUID
    source_name: str | None = None
    source_url: str | None = None
    evidence_count: int = 0
    claim_types_with_evidence: tuple[str, ...] = Field(default_factory=tuple)


class CanonicalOpportunity(BaseModel):
    """The formalized canonical representation Part 1 asks for --
    IDENTITY / CLASSIFICATION / ELIGIBILITY / APPLICATION / FUNDING /
    TEMPORAL / PROVENANCE, assembled read-only from the real,
    already-persisted `Opportunity` row plus its real `Claim` rows.
    """

    model_config = ConfigDict(frozen=True)

    # IDENTITY
    opportunity_id: uuid.UUID
    canonical_url: str
    provider: str | None = None
    title: str

    # CLASSIFICATION
    opportunity_type: OpportunityType
    raw_opportunity_type_label: str | None = None
    country: str | None = None
    region: str | None = None  # not extracted today; always None (honest gap)
    language: str | None = None  # not extracted today; always None (honest gap)
    academic_level: list[AcademicLevel] | None = None

    # ELIGIBILITY / APPLICATION / FUNDING / TEMPORAL / PROVENANCE
    eligibility: CanonicalEligibility
    application_url: str | None = None
    required_documents: str | None = None  # not extracted today; always None (honest gap)
    funding: CanonicalFunding
    temporal: CanonicalTemporal
    provenance: CanonicalProvenance


def build_canonical_opportunity(
    opportunity: Opportunity,
    claims: list[Claim] | None = None,
    *,
    deadline_state: str | None = None,
    funding_type_labels: tuple[str, ...] = (),
    is_verified_at: dt.datetime | None = None,
) -> CanonicalOpportunity:
    """Pure projection: reads an already-persisted `Opportunity` (and its
    already-persisted `Claim` rows, if supplied) into one
    `CanonicalOpportunity`. Performs no DB I/O itself -- the caller
    passes in whatever it already loaded.

    `deadline_state`/`funding_type_labels` are optional, caller-supplied
    classifications (see `deadline_intelligence`/`funding_intelligence`)
    -- this function never classifies text itself, staying a pure
    read-only assembler.
    """
    claims = claims or []
    claims_by_type: dict[str, Claim] = {c.claim_type: c for c in claims}
    evidence_claim_types = tuple(sorted(c.claim_type for c in claims if c.evidence))

    eligibility_claim = claims_by_type.get("eligibility")
    funding_claim = claims_by_type.get("funding")

    application_url = None
    app_url_claim = claims_by_type.get("application_url")
    if app_url_claim is not None:
        application_url = app_url_claim.normalized_value or app_url_claim.claim_value
    elif opportunity.target_url:
        application_url = opportunity.target_url

    return CanonicalOpportunity(
        opportunity_id=opportunity.id,
        canonical_url=opportunity.target_url,
        provider=None,  # resolved via opportunity.source relationship by the caller if needed
        title=opportunity.title,
        opportunity_type=opportunity.type,
        raw_opportunity_type_label=(
            (opportunity.raw_extracted_data or {}).get("raw_opportunity_type_label")
            if isinstance(opportunity.raw_extracted_data, dict)
            else None
        ),
        country=opportunity.country,
        region=None,
        language=None,
        academic_level=opportunity.target_academic_levels,
        eligibility=CanonicalEligibility(
            minimum_age=opportunity.min_age,
            maximum_age=opportunity.max_age,
            academic_requirements=opportunity.target_academic_levels,
            gender=opportunity.target_gender,
            raw_eligibility_text=(
                eligibility_claim.claim_value if eligibility_claim else opportunity.eligibility_criteria
            ),
        ),
        application_url=application_url,
        funding=CanonicalFunding(
            raw_funding_text=(funding_claim.claim_value if funding_claim else opportunity.funding_details),
            funding_type_labels=funding_type_labels,
        ),
        temporal=CanonicalTemporal(
            published_at=None,
            deadline=opportunity.deadline,
            deadline_state=deadline_state,
            status="verified" if opportunity.is_verified else "unverified",
            last_verified_at=is_verified_at,
        ),
        provenance=CanonicalProvenance(
            source_id=opportunity.source_id,
            evidence_count=sum(len(c.evidence) for c in claims),
            claim_types_with_evidence=evidence_claim_types,
        ),
    )
