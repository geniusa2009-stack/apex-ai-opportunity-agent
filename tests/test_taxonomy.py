"""Tests for the centralized canonical taxonomy (Deliverable 5, Feature
4 / `app/services/taxonomy.py`)."""
from app.models.enums import OpportunityType
from app.services.taxonomy import normalize_category, normalize_country


class TestNormalizeCategory:
    def test_canonical_category_maps_to_itself(self):
        assert normalize_category("scholarship") == OpportunityType.SCHOLARSHIP

    def test_alias_normalization_english_synonym(self):
        assert normalize_category("Funded Scholarship") == OpportunityType.SCHOLARSHIP
        assert normalize_category("Internships_Training") == OpportunityType.INTERNSHIP
        assert normalize_category("Self_Development") == OpportunityType.COURSE

    def test_alias_normalization_case_and_whitespace_insensitive(self):
        assert normalize_category("  SCHOLARSHIPS  ") == OpportunityType.SCHOLARSHIP
        assert normalize_category("Volunteer   Opportunities".replace(" Opportunities", "")) == OpportunityType.VOLUNTEER

    def test_multilingual_alias_arabic(self):
        assert normalize_category("منحة") == OpportunityType.SCHOLARSHIP
        assert normalize_category("تدريب") == OpportunityType.INTERNSHIP

    def test_multilingual_alias_french(self):
        assert normalize_category("bourse") == OpportunityType.SCHOLARSHIP
        assert normalize_category("stage") == OpportunityType.INTERNSHIP

    def test_unknown_category_returns_none(self):
        assert normalize_category("something completely unrelated") is None
        assert normalize_category("job posting") is None

    def test_empty_or_none_category_returns_none(self):
        assert normalize_category(None) is None
        assert normalize_category("") is None

    def test_newly_added_alias_participates_immediately(self):
        # Simulates "a newly added category alias" test requirement:
        # adding one dict entry is the entire extension mechanism.
        from app.services import taxonomy

        taxonomy.CATEGORY_ALIASES["____test_alias____"] = OpportunityType.COURSE
        try:
            assert normalize_category("____test_alias____") == OpportunityType.COURSE
        finally:
            del taxonomy.CATEGORY_ALIASES["____test_alias____"]


class TestNormalizeCountry:
    def test_canonical_geography_egypt(self):
        assert normalize_country("Egypt") == "EG"
        assert normalize_country("egypt") == "EG"

    def test_multilingual_country_alias(self):
        assert normalize_country("مصر") == "EG"
        assert normalize_country("Égypte") == "EG"

    def test_global_online_sentinel(self):
        assert normalize_country("Global") == "GLOBAL"
        assert normalize_country("Online") == "GLOBAL"
        assert normalize_country("Worldwide") == "GLOBAL"

    def test_unknown_geography_returns_none(self):
        assert normalize_country("Atlantis") is None

    def test_empty_or_none_geography_returns_none(self):
        assert normalize_country(None) is None
        assert normalize_country("") is None

    def test_phase4_real_vetted_source_countries(self):
        # Phase 4 (Taxonomy / Geography Scalability): these countries
        # are not speculative -- each was named in Phase 3A's own real
        # source-vetting notes for @opcorners/@FullOpportunities, or
        # observed directly in Phase 3B.1's real live-fetched sample
        # text ("Youth Forum on UN Global Goals 2026 Switzerland").
        assert normalize_country("China") == "CN"
        assert normalize_country("الصين") == "CN"
        assert normalize_country("Saudi Arabia") == "SA"
        assert normalize_country("KSA") == "SA"
        assert normalize_country("السعودية") == "SA"
        assert normalize_country("Hong Kong") == "HK"
        assert normalize_country("South Korea") == "KR"
        assert normalize_country("Republic of Korea") == "KR"
        assert normalize_country("Finland") == "FI"
        assert normalize_country("Hungary") == "HU"
        assert normalize_country("Romania") == "RO"
        assert normalize_country("Japan") == "JP"
        assert normalize_country("اليابان") == "JP"
        assert normalize_country("Singapore") == "SG"
        assert normalize_country("Australia") == "AU"
        assert normalize_country("Switzerland") == "CH"
        assert normalize_country("سويسرا") == "CH"
        assert normalize_country("Georgia") == "GE"

    def test_bare_korea_is_not_guessed(self):
        # Deliberate, disclosed non-mapping: bare "Korea" is genuinely
        # ambiguous between North and South Korea with no real signal
        # to disambiguate -- this must stay None, never a guess.
        assert normalize_country("Korea") is None

    def test_existing_pre_phase4_entries_unchanged(self):
        # Confirms the Phase 4 addition is purely additive: every
        # pre-existing D5.1 entry still resolves identically.
        assert normalize_country("France") == "FR"
        assert normalize_country("Kenya") == "KE"
        assert normalize_country("Germany") == "DE"
        assert normalize_country("United States") == "US"
        assert normalize_country("UK") == "GB"
