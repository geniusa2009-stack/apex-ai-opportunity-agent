"""Post performance tracking.

Metrics are recorded as measured (manually by the team or by a platform
API adapter). A metric that was not measured stays NULL -- it is never
treated as 0. `engagement_score` is derived ONLY when `views` is known,
as a weighted engagement rate; otherwise it is NULL and the post simply has
no performance signal in RAG ranking (nothing is invented).

    engagement_rate = (reactions + 2*comments + 3*shares + 2*clicks + 5*applications) / views
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import PostPerformance, Publication

WEIGHTS = {"reactions": 1.0, "comments": 2.0, "shares": 3.0, "clicks": 2.0, "applications": 5.0}


class PerformanceInput(BaseModel):
    views: int | None = Field(default=None, ge=0)
    reactions: int | None = Field(default=None, ge=0)
    comments: int | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)
    clicks: int | None = Field(default=None, ge=0)
    applications: int | None = Field(default=None, ge=0)
    measured_at: dt.datetime | None = None
    measurement_source: str = Field(default="manual", max_length=30)

    @model_validator(mode="after")
    def _at_least_one_metric(self) -> "PerformanceInput":
        if all(getattr(self, name) is None for name in ("views", *WEIGHTS)):
            raise ValueError("at least one metric is required")
        return self


def engagement_score(data: PerformanceInput | PostPerformance) -> float | None:
    views = data.views
    if not views:
        return None
    weighted = sum(weight * (getattr(data, name) or 0) for name, weight in WEIGHTS.items())
    if all(getattr(data, name) is None for name in WEIGHTS):
        return None
    return round(weighted / views, 6)


async def record_performance(db: AsyncSession, publication: Publication, data: PerformanceInput) -> PostPerformance:
    row = PostPerformance(
        publication_id=publication.id,
        opportunity_id=publication.opportunity_id,
        platform=publication.platform,
        views=data.views,
        reactions=data.reactions,
        comments=data.comments,
        shares=data.shares,
        clicks=data.clicks,
        applications=data.applications,
        engagement_score=engagement_score(data),
        measurement_source=data.measurement_source,
        measured_at=data.measured_at or dt.datetime.now(dt.timezone.utc),
    )
    db.add(row)
    await db.flush()
    return row


async def best_scores_by_draft(db: AsyncSession, draft_ids: list[uuid.UUID]) -> dict[uuid.UUID, float]:
    """Best measured engagement score per draft (max over its publications
    and measurements). Drafts without any measured score are absent."""
    if not draft_ids:
        return {}
    rows = (
        await db.execute(
            select(Publication.draft_id, PostPerformance.engagement_score, PostPerformance.measured_at)
            .join(PostPerformance, PostPerformance.publication_id == Publication.id)
            .where(Publication.draft_id.in_(draft_ids), PostPerformance.engagement_score.is_not(None))
            .order_by(PostPerformance.measured_at.asc())
        )
    ).all()
    scores: dict[uuid.UUID, float] = {}
    for draft_id, score, _measured in rows:
        scores[draft_id] = max(scores.get(draft_id, 0.0), score)
    return scores
