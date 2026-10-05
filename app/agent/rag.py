"""Historical RAG for the Writer: retrieval + few-shot + performance-informed
ranking. This is NOT reinforcement learning and changes no model weights;
it only chooses which 1-3 past APEX posts are shown to the Writer as style
examples.

Candidates: drafts that a human APPROVED and that were PUBLISHED (a rejected
or never-published draft is never an example), from a bounded recent
window. Score per candidate:

    score = w_sim * similarity + w_perf * performance + w_rec * recency

* similarity -- cosine of embeddings from the SAME model when both exist,
  otherwise deterministic token Jaccard (always available, zero cost); a
  same-opportunity-type bonus is added
* performance -- best measured engagement score, normalized by the best in
  the window. Posts WITHOUT measurements get no performance term at all
  (weights are renormalized) -- a missing metric is not a zero and is never
  invented
* recency -- exp(-age_days / 120)

The examples are passed to the Writer under "style only" instructions and
every resulting post goes through the deterministic fact guard, so a fact
from an example cannot reach a published post: FACTS > HISTORICAL STYLE.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.normalization import normalize_text
from app.agent.performance import best_scores_by_draft
from app.models.agent import AgentPostDraft
from app.models.agent_enums import DraftStatus
from app.models.opportunity import Opportunity

CANDIDATE_WINDOW = 300
W_SIM, W_PERF, W_REC = 0.6, 0.25, 0.15
MIN_SIMILARITY = 0.05
RECENCY_DAYS = 120.0


@dataclasses.dataclass
class StyleExample:
    draft_id: uuid.UUID
    body: str
    score: float
    similarity: float
    performance: float | None
    recency: float


def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in normalize_text(text).split() if len(t) > 2)


def lexical_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def rank_examples(
    query_text: str,
    query_type: str | None,
    candidates: list[tuple[AgentPostDraft, str | None]],
    performance: dict[uuid.UUID, float],
    *,
    now: dt.datetime,
    query_embedding: dict | None = None,
    k: int = 3,
) -> list[StyleExample]:
    """Pure ranking function (unit-tested directly)."""
    max_perf = max(performance.values(), default=0.0)
    scored: list[StyleExample] = []
    for draft, opp_type in candidates:
        similarity = None
        if query_embedding and draft.embedding and draft.embedding.get("model") == query_embedding.get("model"):
            similarity = max(0.0, cosine(query_embedding.get("vector") or [], draft.embedding.get("vector") or []))
        if similarity is None:
            similarity = lexical_similarity(query_text, draft.body)
        if query_type and opp_type and query_type == opp_type:
            similarity = min(1.0, similarity + 0.1)
        if similarity < MIN_SIMILARITY:
            continue
        age_days = max(0.0, (now - _aware(draft.created_at)).total_seconds() / 86400) if draft.created_at else 0.0
        recency = math.exp(-age_days / RECENCY_DAYS)
        perf = performance.get(draft.id)
        if perf is not None and max_perf > 0:
            perf_norm = perf / max_perf
            score = W_SIM * similarity + W_PERF * perf_norm + W_REC * recency
        else:
            perf_norm = None
            total = W_SIM + W_REC
            score = (W_SIM / total) * similarity + (W_REC / total) * recency
        scored.append(StyleExample(draft.id, draft.body, round(score, 6), round(similarity, 6), perf_norm, round(recency, 6)))
    scored.sort(key=lambda ex: (-ex.score, str(ex.draft_id)))
    return scored[: max(0, min(k, 3))]


async def retrieve_style_examples(
    db: AsyncSession,
    *,
    query_text: str,
    opportunity_type: str | None,
    exclude_opportunity_id: uuid.UUID | None = None,
    query_embedding: dict | None = None,
    k: int = 3,
    now: dt.datetime | None = None,
) -> list[StyleExample]:
    now = now or dt.datetime.now(dt.timezone.utc)
    query = (
        select(AgentPostDraft, Opportunity.type)
        .join(Opportunity, Opportunity.id == AgentPostDraft.opportunity_id)
        .where(AgentPostDraft.status == DraftStatus.PUBLISHED.value)
        .order_by(AgentPostDraft.created_at.desc())
        .limit(CANDIDATE_WINDOW)
    )
    if exclude_opportunity_id is not None:
        query = query.where(AgentPostDraft.opportunity_id != exclude_opportunity_id)
    rows = (await db.execute(query)).all()
    candidates = [(draft, opp_type.value if hasattr(opp_type, "value") else opp_type) for draft, opp_type in rows]
    performance = await best_scores_by_draft(db, [draft.id for draft, _ in candidates])
    return rank_examples(query_text, opportunity_type, candidates, performance, now=now,
                         query_embedding=query_embedding, k=k)
