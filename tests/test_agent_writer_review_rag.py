"""Phases 6, 7, 10, 11 -- APEX Writer (template + LLM with fact guard),
Reviewer/Quality Gate, historical RAG ranking and performance scores."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import pytest_asyncio

from app.agent.extraction import NOT_STATED_AR, ExtractedOpportunity
from app.agent.llm import LLMClient
from app.agent.llm.fake import FakeLLMProvider
from app.agent.performance import PerformanceInput, engagement_score, record_performance
from app.agent.rag import lexical_similarity, rank_examples, retrieve_style_examples
from app.agent.review import quality_gate, review_post
from app.agent.writer import (
    APEX_SIGNATURE,
    PostFacts,
    fact_guard,
    format_date_ar,
    render_template,
    write_post,
)
from app.models.agent import AgentPostDraft, Publication
from app.models.agent_enums import DraftStatus, ReviewDecision
from app.models.enums import OpportunityType
from app.models.opportunity import Opportunity
from tests.agent_support import (
    make_engine_and_session_factory,
    make_source,
    sample_extraction,
)


def facts(**overrides) -> PostFacts:
    return PostFacts(ExtractedOpportunity.model_validate(sample_extraction(**overrides)), verification_level="official_confirmed")


# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------

def test_template_contains_all_verified_facts_and_passes_the_guard():
    f = facts()
    body = render_template(f)
    assert "*DAAD Master's Scholarship in Germany 2027*" in body
    assert "15 أكتوبر 2026" in body
    assert f.extraction.application_url in body
    assert APEX_SIGNATURE in body
    assert fact_guard(body, f).ok


def test_template_never_guesses_missing_facts():
    f = facts(deadline=None, organization=None, funding=None, eligibility=None, application_url=None)
    body = render_template(f)
    assert body.count(NOT_STATED_AR) >= 5
    assert fact_guard(body, f).ok


def test_format_date_ar():
    assert format_date_ar(dt.date(2026, 3, 1)) == "1 مارس 2026"


def test_fact_guard_catches_invented_link_date_and_number():
    f = facts()
    body = render_template(f) + "\nمنحة إضافية 5000 دولار حتى 2026-12-31 https://scam.example.com/x"
    violations = fact_guard(body, f).violations
    assert any("link not in the verified facts" in v for v in violations)
    assert any("date not in the verified facts" in v for v in violations)
    assert any("5000" in v for v in violations)


def test_fact_guard_requires_link_and_deadline():
    f = facts()
    violations = fact_guard("منحة DAAD للماجستير " + APEX_SIGNATURE, f).violations
    assert "the official/application link is missing from the post" in violations
    assert "the deadline is missing from the post" in violations


@pytest.mark.asyncio
async def test_llm_writer_output_is_used_when_it_respects_facts():
    f = facts()
    good = render_template(f).replace("🎓", "✨")
    provider = FakeLLMProvider([{"post_text": good}])
    post = await write_post(f, mode="llm", llm=LLMClient(primary=provider), examples=["old post"])
    assert post.writer_mode == "llm" and post.guard.ok and post.body == good
    prompt = provider.requests[0].messages[0].content
    assert "STYLE EXAMPLES (style only" in prompt and "old post" in prompt


@pytest.mark.asyncio
async def test_llm_writer_hallucination_is_fed_back_then_falls_back_to_template():
    f = facts()
    hallucinated = render_template(f) + "\n🎁 تذكرة طيران مجانية بقيمة 800 يورو https://example.com/bonus"
    provider = FakeLLMProvider([{"post_text": hallucinated}])  # keeps hallucinating
    post = await write_post(f, mode="llm", llm=LLMClient(primary=provider), max_guard_rounds=2)
    assert post.writer_mode == "template_fallback"
    assert post.body == render_template(f) and post.guard.ok
    assert provider.calls == 2
    assert "Fix these problems" in provider.requests[1].messages[0].content


@pytest.mark.asyncio
async def test_examples_cannot_inject_facts():
    """A style example about ANOTHER opportunity leaks its link/date into the
    LLM output -> the fact guard rejects it -> template is used."""
    f = facts()
    example = "منحة Chevening آخر موعد 5 نوفمبر 2026 https://www.chevening.org/apply"
    leaked = render_template(f) + "\nآخر موعد 5 نوفمبر 2026 https://www.chevening.org/apply"
    post = await write_post(f, mode="llm", llm=LLMClient(primary=FakeLLMProvider([{"post_text": leaked}])),
                            examples=[example], max_guard_rounds=1)
    assert post.writer_mode == "template_fallback"
    assert "chevening" not in post.body.lower()


@pytest.mark.asyncio
async def test_template_mode_makes_no_llm_call():
    provider = FakeLLMProvider([{"post_text": "x" * 100}])
    post = await write_post(facts(), mode="template", llm=LLMClient(primary=provider))
    assert post.writer_mode == "template" and provider.calls == 0


# --------------------------------------------------------------------------
# Reviewer / quality gate
# --------------------------------------------------------------------------

def test_quality_gate_approves_a_clean_post():
    f = facts()
    result = quality_gate(render_template(f), f, verification_level="official_confirmed")
    assert result.decision == ReviewDecision.APPROVE
    assert not [i for i in result.issues if i.severity == "error"]


def test_quality_gate_needs_revision_for_unsupported_claims_and_formatting():
    f = facts()
    result = quality_gate(render_template(f) + "\n*مكافأة 100 دولار", f, verification_level="official_confirmed")
    assert result.decision == ReviewDecision.NEEDS_REVISION
    codes = {i.code for i in result.issues}
    assert {"UNSUPPORTED_CLAIM", "BROKEN_FORMATTING"} <= codes
    assert result.revision_notes


def test_quality_gate_rejects_contradicted_and_flags_duplicate_risk_and_missing_info():
    f = facts(deadline=None, eligibility=None)
    contradicted = quality_gate(render_template(f), f, verification_level="contradicted")
    assert contradicted.decision == ReviewDecision.REJECT
    warn = quality_gate(render_template(f), f, verification_level="source_only",
                        dedup_outcome="possible_duplicate", duplicate_of="abc", red_flags=["shortened link"])
    codes = {i.code for i in warn.issues}
    assert {"MISSING_DEADLINE", "UNCLEAR_ELIGIBILITY", "SOURCE_ONLY", "POSSIBLE_DUPLICATE", "RED_FLAG"} <= codes
    assert warn.decision == ReviewDecision.APPROVE  # warnings go to the human, they don't block


@pytest.mark.asyncio
async def test_llm_reviewer_can_only_make_the_verdict_stricter():
    f = facts()
    body = render_template(f)
    rejecting = FakeLLMProvider([{"decision": "reject", "issues": [{"code": "MISLEADING", "message": "overpromises"}]}])
    result = await review_post(body, f, verification_level="official_confirmed", llm=LLMClient(primary=rejecting), use_llm=True)
    assert result.decision == ReviewDecision.REJECT and result.llm_used

    approving = FakeLLMProvider([{"decision": "approve", "issues": []}])
    bad = body + "\nhttps://scam.example.com/x"
    result = await review_post(bad, f, verification_level="official_confirmed", llm=LLMClient(primary=approving), use_llm=True)
    assert result.decision == ReviewDecision.NEEDS_REVISION  # deterministic error stands
    assert approving.calls == 0  # no LLM call spent on a post that already failed the gate


# --------------------------------------------------------------------------
# Performance
# --------------------------------------------------------------------------

def test_engagement_score_only_with_views():
    assert engagement_score(PerformanceInput(views=1000, reactions=50, comments=10, clicks=20)) == pytest.approx(0.11)
    assert engagement_score(PerformanceInput(reactions=50)) is None  # no views -> no invented rate
    with pytest.raises(ValueError):
        PerformanceInput()


# --------------------------------------------------------------------------
# RAG
# --------------------------------------------------------------------------

NOW = dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc)


def _draft(body: str, days_old: int) -> AgentPostDraft:
    return AgentPostDraft(id=uuid.uuid4(), opportunity_id=uuid.uuid4(), body=body, writer_mode="template",
                          status=DraftStatus.PUBLISHED.value, created_at=NOW - dt.timedelta(days=days_old))


def test_rank_examples_uses_similarity_performance_recency_and_caps_at_three():
    similar_good = _draft("منحة ماجستير ممولة بالكامل في ألمانيا DAAD", 30)
    similar_bad = _draft("منحة ماجستير ممولة بالكامل في ألمانيا", 30)
    unrelated = _draft("وظيفة محاسب في شركة", 1)
    extra = [_draft("منحة ماجستير في فرنسا ممولة", d) for d in (5, 6, 7)]
    candidates = [(similar_good, "scholarship"), (similar_bad, "scholarship"), (unrelated, "job")] + [(d, "scholarship") for d in extra]
    perf = {similar_good.id: 0.2, similar_bad.id: 0.01}
    ranked = rank_examples("منحة ماجستير ممولة بالكامل ألمانيا", "scholarship", candidates, perf, now=NOW)
    assert len(ranked) == 3
    assert ranked[0].draft_id == similar_good.id
    assert unrelated.id not in [r.draft_id for r in ranked]
    assert ranked[0].performance == 1.0
    assert any(r.performance is None for r in ranked)  # no metrics -> no invented performance


def test_embeddings_only_compared_within_the_same_model():
    draft = _draft("x", 1)
    draft.embedding = {"model": "model-a", "vector": [1.0, 0.0]}
    same = rank_examples("x", None, [(draft, None)], {}, now=NOW, query_embedding={"model": "model-a", "vector": [1.0, 0.0]})
    other = rank_examples("y", None, [(draft, None)], {}, now=NOW, query_embedding={"model": "model-b", "vector": [1.0, 0.0]})
    assert same and same[0].similarity == 1.0
    assert not other  # falls back to lexical similarity ("y" vs "x" = 0) -> filtered


def test_lexical_similarity():
    assert lexical_similarity("منحة ماجستير المانيا", "منحة ماجستير ألمانيا") == 1.0


@pytest_asyncio.fixture
async def session():
    engine, factory = await make_engine_and_session_factory()
    async with factory() as db:
        yield db
    await engine.dispose()


@pytest.mark.asyncio
async def test_retrieve_only_published_drafts_with_measured_performance(session):
    source = await make_source(session, source_type="website")

    async def opp():
        o = Opportunity(title="t", target_url="https://e.org/x", source_id=source.id, type=OpportunityType.SCHOLARSHIP)
        session.add(o)
        await session.flush()
        return o

    o1, o2, o3 = await opp(), await opp(), await opp()
    published = AgentPostDraft(opportunity_id=o1.id, body="منحة ماجستير ممولة ألمانيا", writer_mode="template",
                               status=DraftStatus.PUBLISHED.value)
    rejected = AgentPostDraft(opportunity_id=o2.id, body="منحة ماجستير ممولة ألمانيا مرفوضة", writer_mode="template",
                              status=DraftStatus.REJECTED.value)
    waiting = AgentPostDraft(opportunity_id=o3.id, body="منحة ماجستير ممولة ألمانيا انتظار", writer_mode="template",
                             status=DraftStatus.WAITING_APPROVAL.value)
    session.add_all([published, rejected, waiting])
    await session.flush()
    pub = Publication(idempotency_key="k" * 64, draft_id=published.id, opportunity_id=o1.id, platform="mock",
                      status="published")
    session.add(pub)
    await session.flush()
    await record_performance(session, pub, PerformanceInput(views=100, reactions=10))
    examples = await retrieve_style_examples(session, query_text="منحة ماجستير ألمانيا", opportunity_type="scholarship")
    assert [e.draft_id for e in examples] == [published.id]
    assert examples[0].performance == 1.0
