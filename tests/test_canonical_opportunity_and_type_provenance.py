"""Opportunity Intelligence Hardening -- Part 1 (Canonical Opportunity
Contract) and the raw-type-label-provenance fix (Part 2's "preserve the
raw source label" requirement).

UNIT tier: Part 1 tests construct `Opportunity`/`Claim` ORM objects
in-memory (no DB session, no I/O -- `build_canonical_opportunity` is a
pure function over already-loaded objects). Part 2 tests mock only the
LLM call (`_extract_fields_via_gemini`), same established convention as
every other test file in this repository, and prove the real
`process_raw_post_via_ai` pipeline function preserves the raw label.
"""
from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.claim import Claim
from app.models.evidence import Evidence
from app.models.enums import EvidenceStatus, OpportunityType, SourceType
from app.models.opportunity import Opportunity
from app.schemas.opportunity import OpportunityCreate
from app.services.aggregator_service import AIExtractedOpportunityFields, process_raw_post_via_ai
from app.services.canonical_opportunity import build_canonical_opportunity


def _make_opportunity(**overrides) -> Opportunity:
    defaults = dict(
        id=uuid.uuid4(),
        title="Sample Program",
        description="A sample opportunity.",
        target_url="https://example.org/apply",
        source_id=uuid.uuid4(),
        type=OpportunityType.SCHOLARSHIP,
        deadline=None,
        eligibility_criteria=None,
        funding_details=None,
        country="Global",
        is_verified=False,
        is_local_egypt=False,
        target_age_group=None,
        target_academic_levels=None,
        min_age=None,
        max_age=None,
        target_gender=None,
        raw_extracted_data=None,
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


def _make_claim(opportunity_id, claim_type, claim_value, *, evidence=None) -> Claim:
    claim = Claim(
        id=uuid.uuid4(),
        opportunity_id=opportunity_id,
        claim_type=claim_type,
        claim_value=claim_value,
        normalized_value=None,
        status=EvidenceStatus.UNKNOWN.value,
        importance="normal",
    )
    # Claim.evidence is a relationship; assign a plain list directly for
    # this in-memory (non-session-attached) construction.
    claim.evidence = evidence or []
    return claim


class TestCanonicalOpportunityContract:
    def test_unknown_fields_stay_none_never_guessed(self):
        opp = _make_opportunity()
        canonical = build_canonical_opportunity(opp, claims=[])
        # Fields this codebase genuinely cannot extract today.
        assert canonical.region is None
        assert canonical.language is None
        assert canonical.eligibility.nationality is None
        assert canonical.eligibility.residency is None
        assert canonical.eligibility.field_or_discipline is None
        assert canonical.eligibility.experience_requirements is None
        assert canonical.temporal.source_updated_at is None
        assert canonical.required_documents is None

    def test_identity_and_classification_populated_from_real_row(self):
        opp = _make_opportunity(title="Winter Camp", type=OpportunityType.INTERNSHIP, country="Egypt")
        canonical = build_canonical_opportunity(opp, claims=[])
        assert canonical.opportunity_id == opp.id
        assert canonical.canonical_url == opp.target_url
        assert canonical.title == "Winter Camp"
        assert canonical.opportunity_type == OpportunityType.INTERNSHIP
        assert canonical.country == "Egypt"

    def test_eligibility_pulled_from_real_age_and_gender_columns(self):
        opp = _make_opportunity(min_age=18, max_age=25)
        canonical = build_canonical_opportunity(opp, claims=[])
        assert canonical.eligibility.minimum_age == 18
        assert canonical.eligibility.maximum_age == 25

    def test_application_url_falls_back_to_target_url_when_no_claim(self):
        opp = _make_opportunity(target_url="https://real.org/apply")
        canonical = build_canonical_opportunity(opp, claims=[])
        assert canonical.application_url == "https://real.org/apply"

    def test_application_url_prefers_claim_normalized_value(self):
        opp = _make_opportunity(target_url="https://real.org/apply")
        claim = _make_claim(opp.id, "application_url", "https://real.org/apply")
        claim.normalized_value = "https://real.org/apply?utm=stripped"
        canonical = build_canonical_opportunity(opp, claims=[claim])
        assert canonical.application_url == "https://real.org/apply?utm=stripped"

    def test_provenance_evidence_count_and_claim_types_reflect_real_evidence(self):
        opp = _make_opportunity()
        claim_with_evidence = _make_claim(opp.id, "deadline", "2026-12-01")
        ev1 = Evidence(
            id=uuid.uuid4(), opportunity_id=opp.id, claim_id=claim_with_evidence.id,
            source_url="https://real.gov/x", evidence_text="Deadline: Dec 1, 2026",
            evidence_hash="deadbeef", supports_claim=True,
        )
        claim_with_evidence.evidence = [ev1]
        claim_without_evidence = _make_claim(opp.id, "eligibility", "Open to all")
        canonical = build_canonical_opportunity(opp, claims=[claim_with_evidence, claim_without_evidence])
        assert canonical.provenance.evidence_count == 1
        assert canonical.provenance.claim_types_with_evidence == ("deadline",)

    def test_temporal_status_reflects_real_is_verified_flag(self):
        unverified = _make_opportunity(is_verified=False)
        verified = _make_opportunity(is_verified=True)
        assert build_canonical_opportunity(unverified, claims=[]).temporal.status == "unverified"
        assert build_canonical_opportunity(verified, claims=[]).temporal.status == "verified"

    def test_deadline_state_and_funding_labels_are_caller_supplied_never_self_computed(self):
        opp = _make_opportunity()
        canonical = build_canonical_opportunity(
            opp, claims=[], deadline_state="rolling", funding_type_labels=("stipend", "travel_funded")
        )
        assert canonical.temporal.deadline_state == "rolling"
        assert canonical.funding.funding_type_labels == ("stipend", "travel_funded")

    def test_raw_opportunity_type_label_read_from_raw_extracted_data(self):
        opp = _make_opportunity(raw_extracted_data={"raw_opportunity_type_label": "fellowship"})
        canonical = build_canonical_opportunity(opp, claims=[])
        assert canonical.raw_opportunity_type_label == "fellowship"

    def test_is_not_a_second_persisted_model(self):
        # Static proof this module never imports a DB session type or
        # issues a query -- it's a pure projection, per Part 20.
        import inspect
        from app.services import canonical_opportunity as mod
        src = inspect.getsource(mod)
        assert "AsyncSession" not in src
        assert "db.execute" not in src
        assert "db.add" not in src


class TestRawOpportunityTypeLabelProvenance:
    """Confirms the real, disclosed information-loss bug found during
    Part 2's audit is fixed: `_coerce_opportunity_type`'s fallback used
    to silently discard Gemini's own raw category string. This is now
    preserved verbatim on `OpportunityCreate.raw_opportunity_type_label`,
    which `raw_extracted_data=data.model_dump(mode="json")` persists
    unchanged -- zero migration, zero new column."""

    @pytest.mark.asyncio
    async def test_unmappable_category_label_is_preserved_even_though_type_falls_back(self):
        mocked_fields = AIExtractedOpportunityFields(
            title="Global Innovators Fellowship",
            description="A competitive fellowship for young innovators.",
            original_link="https://example.org/fellowship",
            deadline=None,
            country_hint="Global",
            target_age_group=None,
            opportunity_type="fellowship",  # not one of the 4 canonical values
        )
        with patch(
            "app.services.aggregator_service._extract_fields_via_gemini",
            new=AsyncMock(return_value=mocked_fields),
        ):
            result = await process_raw_post_via_ai(
                "Apply now for the Global Innovators Fellowship!",
                {"source_name": "Test Source", "source_type": SourceType.WEBSITE, "source_base_url": "https://example.org"},
            )
        assert isinstance(result, OpportunityCreate)
        # The enum itself correctly falls back (unchanged behavior).
        assert result.opportunity_type in OpportunityType
        # But the raw label Gemini actually said is preserved, not lost.
        assert result.raw_opportunity_type_label == "fellowship"
        # And it survives into the exact JSON blob that gets persisted
        # as Opportunity.raw_extracted_data.
        dumped = result.model_dump(mode="json")
        assert dumped["raw_opportunity_type_label"] == "fellowship"

    @pytest.mark.asyncio
    async def test_cleanly_mapped_category_label_is_still_preserved(self):
        mocked_fields = AIExtractedOpportunityFields(
            title="Summer Internship",
            description="A summer internship program.",
            original_link="https://example.org/intern",
            deadline=None,
            country_hint="Global",
            target_age_group=None,
            opportunity_type="internship",
        )
        with patch(
            "app.services.aggregator_service._extract_fields_via_gemini",
            new=AsyncMock(return_value=mocked_fields),
        ):
            result = await process_raw_post_via_ai(
                "Join our summer internship!",
                {"source_name": "Test Source", "source_type": SourceType.WEBSITE, "source_base_url": "https://example.org"},
            )
        assert result.opportunity_type == OpportunityType.INTERNSHIP
        assert result.raw_opportunity_type_label == "internship"
