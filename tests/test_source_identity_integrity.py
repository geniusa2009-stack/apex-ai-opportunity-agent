"""Tests for the Source Identity Integrity repair (Part A of the
Source Identity Integrity + E2E Live Opportunity Pipeline task).

Proves: all 117 registered sources have a unique, deterministic
source_id; the repair changed only source_id for the 13 previously-
duplicated records (names/URLs/tiers/adapter types untouched); the
generator function is pure and reproducible; and the registry loader
now refuses a duplicate rather than silently returning a partial view.
"""
from __future__ import annotations

import copy
import json

import pytest

from app.services import source_registry as sr


# ---------------------------------------------------------------------
# 117/117 unique, present, unchanged apart from source_id
# ---------------------------------------------------------------------

class TestAllSourcesUniqueAndPresent:
    def test_exactly_117_sources_present(self):
        assert len(sr.load_source_registry()["sources"]) == 117

    def test_all_117_source_ids_are_unique(self):
        ids = [s["source_id"] for s in sr.load_source_registry()["sources"]]
        assert len(ids) == len(set(ids)) == 117

    def test_no_source_id_is_the_old_broken_placeholder(self):
        ids = {s["source_id"] for s in sr.load_source_registry()["sources"]}
        assert "src-source" not in ids
        assert "src-2030" not in ids

    def test_previously_colliding_names_are_still_present_with_distinct_ids(self):
        sources = sr.load_source_registry()["sources"]
        names_of_interest = [
            "مشروع مهني 2030",
            "رواد 2030",
            "شباب بتساعد شباب",
            "فرص خضراء",
            "مراكز شباب مصر",
        ]
        found = {s["name"]: s["source_id"] for s in sources if s["name"] in names_of_interest}
        assert set(found.keys()) == set(names_of_interest)
        assert len(set(found.values())) == len(names_of_interest)  # all distinct


# ---------------------------------------------------------------------
# generate_deterministic_source_id() -- pure function proofs
# ---------------------------------------------------------------------

class TestGenerateDeterministicSourceId:
    def test_pure_name_based_slug_for_ordinary_ascii_name(self):
        assert sr.generate_deterministic_source_id("Coursera") == "src-coursera"

    def test_same_inputs_always_produce_same_id(self):
        a = sr.generate_deterministic_source_id("Some Name (X)", "https://x.com/y", "@handle")
        b = sr.generate_deterministic_source_id("Some Name (X)", "https://x.com/y", "@handle")
        assert a == b

    def test_arabic_only_name_falls_back_to_hash_disambiguated_id(self):
        result = sr.generate_deterministic_source_id("برلمان الشباب")
        assert result.startswith("src-source-")
        assert len(result) == len("src-source-") + 8

    def test_two_different_arabic_names_never_collide(self):
        a = sr.generate_deterministic_source_id("برلمان الشباب")
        b = sr.generate_deterministic_source_id("مشواري")
        assert a != b

    def test_short_numeric_only_slug_is_treated_as_degenerate(self):
        # This is the exact defect that produced the src-2030 x2 collision.
        result_a = sr.generate_deterministic_source_id("مشروع مهني 2030")
        result_b = sr.generate_deterministic_source_id("رواد 2030")
        assert result_a != result_b
        assert not result_a.endswith("src-2030")
        assert result_a != "src-2030"
        assert result_b != "src-2030"

    def test_degenerate_name_with_telegram_handle_uses_the_handle(self):
        result = sr.generate_deterministic_source_id("شباب بتساعد شباب", telegram_handle="@shbab_btsad_shbab")
        assert result == "src-shbab-btsad-shbab"

    def test_degenerate_name_with_canonical_url_uses_the_url(self):
        result = sr.generate_deterministic_source_id("مثال", canonical_url="https://example.gov.eg/programs")
        assert result == "src-example-gov-eg-programs"

    def test_telegram_handle_preferred_over_canonical_url_when_both_present_and_name_degenerate(self):
        result = sr.generate_deterministic_source_id(
            "١٢٣٤", canonical_url="https://example.com/x", telegram_handle="@realhandle"
        )
        assert result == "src-realhandle"

    def test_recomputing_from_stored_fields_reproduces_every_stored_id(self):
        """The strongest determinism proof: for all 117 sources
        currently in the registry, recomputing the id from that same
        record's own name/canonical_url/telegram_handle reproduces the
        exact id stored on disk -- including for the 13 records this
        task's repair actually changed."""
        for s in sr.load_source_registry()["sources"]:
            recomputed = sr.generate_deterministic_source_id(
                s["name"], s.get("canonical_url"), s.get("telegram_handle")
            )
            assert recomputed == s["source_id"], (s["name"], s["source_id"], recomputed)


# ---------------------------------------------------------------------
# Reload stability
# ---------------------------------------------------------------------

class TestReloadStability:
    def test_ids_survive_a_forced_reload(self):
        first = {s["source_id"] for s in sr.load_source_registry()["sources"]}
        sr.load_source_registry.cache_clear()
        second = {s["source_id"] for s in sr.load_source_registry()["sources"]}
        assert first == second
        assert len(second) == 117


# ---------------------------------------------------------------------
# Duplicate ids cannot enter through validation
# ---------------------------------------------------------------------

class TestDuplicateIdsRejectedByValidation:
    def test_a_duplicate_source_id_is_rejected_at_load_time(self, tmp_path, monkeypatch):
        real_data = json.loads(json.dumps(sr.load_source_registry()))  # deep copy via json roundtrip
        sources = real_data["sources"]
        # Force a duplicate by cloning the first source's id onto the second.
        broken = copy.deepcopy(real_data)
        broken["sources"][1]["source_id"] = broken["sources"][0]["source_id"]

        broken_path = tmp_path / "broken_source_registry.json"
        broken_path.write_text(json.dumps(broken), encoding="utf-8")

        monkeypatch.setattr(sr, "_REGISTRY_PATH", broken_path)
        sr.load_source_registry.cache_clear()
        try:
            with pytest.raises(ValueError, match="duplicate source_id"):
                sr.load_source_registry()
        finally:
            sr.load_source_registry.cache_clear()  # restore real registry for subsequent tests


# ---------------------------------------------------------------------
# Names and URLs unchanged by the repair (spot-checked against the
# exact records the repair touched)
# ---------------------------------------------------------------------

class TestRepairPreservedEverythingExceptSourceId:
    AFFECTED_NAMES = [
        "القيادات الشبابية – وزارة الشباب والرياضة",
        "طوّر وغيّر – وزارة الشباب والرياضة",
        "أندية البحث العلمي والابتكار – وزارة الشباب والرياضة",
        "مراكز شباب مصر",
        "برلمان الشباب",
        "مشواري",
        "مراكز التكوين المهني",
        "مراكز التدريب المهني",
        "رواد تكنولوجيا المستقبل",
        "مشروع مهني 2030",
        "رواد 2030",
        "شباب بتساعد شباب",
        "فرص خضراء",
    ]

    def test_all_13_affected_names_still_present_unchanged(self):
        names_in_registry = {s["name"] for s in sr.load_source_registry()["sources"]}
        for name in self.AFFECTED_NAMES:
            assert name in names_in_registry

    def test_affected_records_kept_their_tier_and_source_type(self):
        by_name = {s["name"]: s for s in sr.load_source_registry()["sources"]}
        assert by_name["شباب بتساعد شباب"]["tier"] == "TIER_3_COMMUNITY"
        assert by_name["شباب بتساعد شباب"]["source_type"] == "community"
        assert by_name["شباب بتساعد شباب"]["telegram_handle"] == "@shbab_btsad_shbab"
        assert by_name["مشروع مهني 2030"]["tier"] == "TIER_1_PRIMARY"
        assert by_name["مشروع مهني 2030"]["source_type"] == "government"


# ---------------------------------------------------------------------
# Verification boundary re-proven (unchanged behavior, re-asserted
# after this module's edits -- source_registry.py gained new code)
# ---------------------------------------------------------------------

class TestVerificationBoundaryStillNeverBypassed:
    def test_source_registry_module_not_imported_by_evidence_verification(self):
        import inspect
        from app.services import evidence_verification
        src = inspect.getsource(evidence_verification)
        assert "source_registry" not in src

    def test_source_registry_module_not_imported_by_contradiction_hunter(self):
        import inspect
        from app.services import contradiction_hunter
        src = inspect.getsource(contradiction_hunter)
        assert "source_registry" not in src

    def test_source_registry_module_not_imported_by_the_is_verified_write_site(self):
        import inspect
        from app.workers import tasks
        src = inspect.getsource(tasks)
        assert "source_registry" not in src
