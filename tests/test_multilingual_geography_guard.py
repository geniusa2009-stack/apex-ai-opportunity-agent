"""Opportunity Intelligence Hardening -- Parts 6 (Geography) and 15
(Multi-Language Extraction).

Confirms real, existing guard rails (D5/D5.1's own disclosed design,
audited in Part 0) rather than reintroducing new ones: `country_hint`
is a discovery-signal-only field the extraction prompt explicitly
forbids guessing from language/org-name/source-channel; `country`
defaults to "Global" (not a guessed country) when the source states no
geography; `taxonomy.normalize_country`/`normalize_category` never
infer from anything but the literal, already-extracted text.

INTEGRATION tier for the process_raw_post_via_ai tests (mocks only the
LLM call, same established convention as every other test file in this
repository); UNIT tier for the direct taxonomy.py calls.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.models.enums import SourceType
from app.services.aggregator_service import AIExtractedOpportunityFields, process_raw_post_via_ai
from app.services.taxonomy import normalize_category, normalize_country


def _mocked(**overrides):
    defaults = dict(
        title="Sample",
        description="Sample description.",
        original_link="https://example.org/apply",
        deadline=None,
        country_hint=None,
        target_age_group=None,
        opportunity_type="scholarship",
    )
    defaults.update(overrides)
    return AIExtractedOpportunityFields(**defaults)


async def _run(mocked_fields):
    with patch(
        "app.services.aggregator_service._extract_fields_via_gemini",
        new=AsyncMock(return_value=mocked_fields),
    ):
        return await process_raw_post_via_ai(
            "irrelevant raw text for this mocked path",
            {"source_name": "Test Source", "source_type": SourceType.WEBSITE, "source_base_url": "https://example.org"},
        )


class TestGeographyProviderVsEligibilityVsLanguage:
    @pytest.mark.asyncio
    async def test_arabic_content_with_no_stated_country_defaults_to_global_not_egypt(self):
        # The Arabic-language TEXT itself must never imply Egypt -- only
        # an EXPLICIT country_hint the extraction step actually returned
        # may set country away from the "Global" default.
        result = await _run(_mocked(
            title="منحة دراسية للطلاب المتفوقين",
            description="منحة دراسية متاحة لجميع الطلاب حول العالم",
            country_hint=None,
        ))
        assert result.country == "Global"
        assert result.is_local_egypt is False

    @pytest.mark.asyncio
    async def test_arabic_content_explicitly_naming_kenya_is_kenya_not_egypt(self):
        # Arabic text CAN name a real non-Egypt country -- when it does,
        # that explicit signal is honored, proving the pipeline isn't
        # secretly hardcoded to "Arabic implies Egypt."
        result = await _run(_mocked(
            title="فرصة تدريب في كينيا",
            description="برنامج تدريبي في نيروبي، كينيا",
            country_hint="Kenya",
        ))
        assert result.country == "Kenya"
        assert result.is_local_egypt is False
        assert normalize_country(result.country) == "KE"

    @pytest.mark.asyncio
    async def test_french_content_with_no_stated_country_does_not_default_to_france(self):
        result = await _run(_mocked(
            title="Bourse d'études pour étudiants internationaux",
            description="Programme ouvert aux étudiants du monde entier",
            country_hint=None,
        ))
        # French language must never imply France -- same "Global"
        # default as every other language when nothing is explicitly
        # stated.
        assert result.country == "Global"

    @pytest.mark.asyncio
    async def test_french_content_explicitly_naming_a_country_is_honored(self):
        result = await _run(_mocked(
            title="Stage professionnel en France",
            description="Un stage de six mois à Paris",
            country_hint="France",
        ))
        assert result.country == "France"
        assert normalize_country(result.country) == "FR"

    @pytest.mark.asyncio
    async def test_english_content_provider_country_does_not_imply_eligibility_country(self):
        # An Egyptian-provider post about a program open to non-Egyptians
        # must not silently become an Egypt-eligibility opportunity --
        # this pipeline has no "provider country" concept fed into
        # `country` at all; `country` is exclusively the extraction
        # step's own eligibility/geography signal.
        result = await _run(_mocked(
            title="Global scholarship offered by an Egyptian foundation",
            description="Open to applicants worldwide, hosted by a Cairo-based NGO",
            country_hint="Global",
        ))
        assert result.country == "Global"
        assert result.is_local_egypt is False

    @pytest.mark.asyncio
    async def test_us_provider_egypt_only_eligibility_is_egypt_not_us(self):
        result = await _run(_mocked(
            title="US-funded program, Egypt applicants only",
            description="Funded by a US foundation, but restricted to Egyptian nationals",
            country_hint="Egypt",
        ))
        assert result.country == "Egypt"
        assert result.is_local_egypt is True

    @pytest.mark.asyncio
    async def test_online_remote_content_maps_to_global_sentinel_not_a_guessed_country(self):
        result = await _run(_mocked(title="Online course", country_hint="Online"))
        assert normalize_country(result.country) == "GLOBAL"


class TestMultilingualCategoryNormalization:
    def test_english_arabic_french_synonyms_map_to_the_same_canonical_type(self):
        from app.models.enums import OpportunityType
        assert normalize_category("Scholarship") == OpportunityType.SCHOLARSHIP
        assert normalize_category("منحة دراسية") == OpportunityType.SCHOLARSHIP
        assert normalize_category("bourse d'études") == OpportunityType.SCHOLARSHIP

    def test_unrecognized_category_in_any_language_returns_none_not_a_guess(self):
        assert normalize_category("some entirely made up category xyz123") is None
        assert normalize_category("فئة غير معروفة تماما") is None

    def test_category_normalization_is_case_and_whitespace_insensitive(self):
        from app.models.enums import OpportunityType
        assert normalize_category("  SCHOLARSHIP  ") == OpportunityType.SCHOLARSHIP
        assert normalize_category("ScHoLaRsHiP") == OpportunityType.SCHOLARSHIP
