"""Unit tests for the real, deterministic RIASEC scoring logic in
`app/services/career_compass_service.py`. Pure function, no DB/HTTP
needed -- known inputs, known expected outputs.
"""

import pytest

from app.services.career_compass_service import (
    CAREER_MAP,
    DIMENSIONS,
    NUM_QUESTIONS,
    QUESTIONS,
    InvalidAnswersError,
    questions_public,
    score_riasec,
)


class TestQuestionBank:
    def test_exactly_ten_questions(self):
        assert len(QUESTIONS) == NUM_QUESTIONS

    def test_every_question_has_six_options_in_riasec_order(self):
        for q in QUESTIONS:
            assert len(q["options"]) == len(DIMENSIONS) == 6

    def test_questions_public_shape_has_no_scoring_internals(self):
        out = questions_public()
        assert len(out) == NUM_QUESTIONS
        for q in out:
            assert set(q.keys()) == {"id", "text_ar", "text_en", "options"}
            assert len(q["options"]) == 6


class TestScoreRiasecAllOneDimension:
    def test_all_realistic_answers_score_100_percent_realistic(self):
        # Every answer picks index 0 -> "R" (Realistic) for all 10 Qs.
        result = score_riasec([0] * NUM_QUESTIONS)
        assert result.dimension_scores == {"R": 10, "I": 0, "A": 0, "S": 0, "E": 0, "C": 0}
        assert result.match_percentage == 100.0
        assert result.top_dimensions == ("R",)
        assert result.holland_code[0] == "R"

    def test_all_investigative_answers(self):
        result = score_riasec([1] * NUM_QUESTIONS)
        assert result.dimension_scores["I"] == 10
        assert result.match_percentage == 100.0
        assert result.top_dimensions == ("I",)
        assert result.matched_careers == CAREER_MAP["I"]

    def test_all_conventional_answers(self):
        result = score_riasec([5] * NUM_QUESTIONS)
        assert result.dimension_scores["C"] == 10
        assert result.holland_code[0] == "C"
        assert result.matched_careers == CAREER_MAP["C"]


class TestScoreRiasecMixed:
    def test_known_mixed_distribution(self):
        # 4 x I (index 1), 3 x E (index 4), 2 x A (index 2), 1 x C (index 5).
        answers = [1, 1, 1, 1, 4, 4, 4, 2, 2, 5]
        result = score_riasec(answers)
        assert result.dimension_scores == {"R": 0, "I": 4, "A": 2, "S": 0, "E": 3, "C": 1}
        assert result.match_percentage == 40.0  # 4/10
        assert result.top_dimensions == ("I",)
        assert result.holland_code == "IEA"  # ranked desc: I(4) > E(3) > A(2)
        assert result.matched_careers == CAREER_MAP["I"]

    def test_exact_tie_for_first_includes_both_dimensions_in_matched_careers(self):
        # 5 x R (index 0), 5 x I (index 1) -- exact tie at the top.
        answers = [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
        result = score_riasec(answers)
        assert result.dimension_scores["R"] == 5
        assert result.dimension_scores["I"] == 5
        assert result.match_percentage == 50.0
        assert set(result.top_dimensions) == {"R", "I"}
        # Ties broken by fixed R,I,A,S,E,C order -> R ranked ahead of I.
        assert result.holland_code.startswith("RI")
        assert result.matched_careers == CAREER_MAP["R"] + CAREER_MAP["I"]

    def test_even_spread_across_all_six_dimensions(self):
        # One answer per dimension for the first 6 questions, then repeat
        # R, I, A, S for the remaining 4 -> R:2, I:2, A:2, S:2, E:1, C:1.
        answers = [0, 1, 2, 3, 4, 5, 0, 1, 2, 3]
        result = score_riasec(answers)
        assert sum(result.dimension_scores.values()) == NUM_QUESTIONS
        assert result.dimension_scores == {"R": 2, "I": 2, "A": 2, "S": 2, "E": 1, "C": 1}
        assert result.match_percentage == 20.0
        assert set(result.top_dimensions) == {"R", "I", "A", "S"}


class TestScoreRiasecValidation:
    def test_rejects_wrong_length(self):
        with pytest.raises(InvalidAnswersError):
            score_riasec([0, 1, 2])

    def test_rejects_empty_list(self):
        with pytest.raises(InvalidAnswersError):
            score_riasec([])

    def test_rejects_out_of_range_index(self):
        with pytest.raises(InvalidAnswersError):
            score_riasec([0, 1, 2, 3, 4, 5, 6, 0, 1, 2])  # 6 is out of range (0-5)

    def test_rejects_negative_index(self):
        with pytest.raises(InvalidAnswersError):
            score_riasec([-1, 1, 2, 3, 4, 5, 0, 1, 2, 3])

    def test_rejects_non_int_entries(self):
        with pytest.raises(InvalidAnswersError):
            score_riasec([0, 1, 2, 3, 4, 5, 0, 1, 2, "3"])

    def test_rejects_non_list_input(self):
        with pytest.raises(InvalidAnswersError):
            score_riasec("0123450123")

    def test_rejects_bool_masquerading_as_int(self):
        # bool is a subclass of int in Python -- explicitly rejected so a
        # stray `True`/`False` never silently scores as 1/0.
        with pytest.raises(InvalidAnswersError):
            score_riasec([True, 1, 2, 3, 4, 5, 0, 1, 2, 3])


class TestCareerMapCompleteness:
    def test_every_dimension_has_a_career_mapping(self):
        assert set(CAREER_MAP.keys()) == set(DIMENSIONS)

    def test_every_dimension_maps_to_four_real_bilingual_careers(self):
        for dim, careers in CAREER_MAP.items():
            assert len(careers) == 4
            for c in careers:
                assert c["name_ar"] and c["name_en"]
