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
