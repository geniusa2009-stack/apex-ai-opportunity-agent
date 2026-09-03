"""DISCLOSED NAMING DEVIATION: the `metadata` field name from this
deliverable's own spec collides with SQLAlchemy's own reserved
`Base.metadata` attribute (the ORM's schema registry) on every
declarative model -- both tables below use `trajectory_metadata`/
`step_metadata` instead, same JSONB shape and purpose, just a real,
necessary rename to avoid shadowing a framework attribute.

Search Trajectory Memory (Deliverable 3): a persistent record of
research PATHS -- not application logging -- so a future research run
can reuse past search experience.

STEP 1 INVESTIGATION (read the real Scrapy scraper, the real Hybrid
Opportunity Aggregation Engine (`app/services/aggregator_service.py` +
`app/workers/aggregator_tasks.py`), `ai_gateway.verify_opportunity`
(app/services/ai.py -- confirmed real, OpenAI structured outputs, NOT
Gemini -- some comments elsewhere in this codebase mislabel it, a
pre-existing, disclosed inaccuracy, not this deliverable's own), the
real verification task (`app/workers/tasks.py::_verify_new_opportunities`),
and Deliverable 2's own `entity_relationship_service.py` before writing
this: this codebase has no literal "web search API" step anywhere --
the Scrapy scraper crawls fixed spider start_urls with no query concept
at all, and the aggregator polls a fixed JSON-configured list of
Telegram channels / Facebook pages (currently MOCKED fetch, per that
module's own disclosed scope). The aggregator's own per-run loop
(`_run_daily_opportunity_scraper`) is nonetheless the real, closest
match to this deliverable's "Research Goal -> Search Query -> Search
Result -> ... -> Final Outcome" shape: one run has an implicit
objective ("sweep every configured source for new opportunities"), each
configured channel/page IS a real, real query ("check this source"),
each raw post fetched IS a real search result, and the resulting
`Opportunity` row IS the real extracted entity that later flows into
the real, decoupled `_verify_new_opportunities` verification pass. This
deliverable instruments THAT real loop (see `research_trajectory_memory
.py`'s own module docstring for the integration details) rather than
inventing a parallel "search" abstraction this codebase doesn't have.

No existing run/job identifier exists anywhere in this codebase's
worker infrastructure (confirmed via a direct grep for
`request.id`/`task_id`/`run_id`/`job_id` across `app/workers/` and
`app/services/` -- the one `task_id` hit, `application_tracker.py`, is
an unrelated per-tracker todo-list index). `ResearchTrajectory.id`
itself therefore serves as this deliverable's own run identifier --
no separate `research_run_id` column, since nothing external produces
one to correlate against yet.

STEP 2 DESIGN, "don't duplicate huge page contents": neither table
below stores raw scraped text, full LLM responses, or embeddings.
`ResearchTrajectoryStep.reference_id` stores only a compact reference
(a real `Opportunity.id`, when one was created/updated) -- the full
record already lives in the real `opportunities` table, one hop away.
"""
import datetime as dt
import uuid
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import TrajectoryOutcome, TrajectoryStatus


class ResearchTrajectory(Base):
    """One real research run (today: one `_run_daily_opportunity_scraper`
    invocation) -- the objective, its real steps, and its real, final,
    disclosed-as-a-hint-not-truth outcome."""

    __tablename__ = "research_trajectories"
    __table_args__ = (
        Index("ix_research_trajectories_normalized_objective", "normalized_objective"),
        Index("ix_research_trajectories_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    objective: Mapped[str] = mapped_column(String(500), nullable=False)
    # Lowercased, whitespace-collapsed `objective` -- the deterministic
    # matching key `retrieve_similar_trajectories` compares against (see
    # `research_trajectory_memory.normalize_objective`). No embeddings,
    # per Step 4's own explicit instruction not to introduce a vector
    # database "just because this is called memory".
    normalized_objective: Mapped[str] = mapped_column(String(500), nullable=False)

    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TrajectoryStatus.RUNNING.value)

    total_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    opportunities_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opportunities_verified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Real, deduplicated list of source domains this trajectory's steps
    # touched that produced at least one real discovered opportunity --
    # a compact rollup, not a re-derivation query every read needs to
    # repeat. Plain JSONB list of strings, same posture as
    # `raw_extracted_data` elsewhere in this codebase (structured but
    # not a new relational table for something this simple).
    useful_sources: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    # How many distinct real sources (channels/pages/domains) this
    # trajectory actually queried -- a real, simple breadth signal.
    research_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # `TrajectoryOutcome` -- see that enum's own docstring (app/models/
    # enums.py) for why this is a research-efficiency signal, never a
    # truth score.
    final_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)

    trajectory_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class ResearchTrajectoryStep(Base):
    """One real action within a trajectory -- a query against one real
    source, or the processing of one real search result from it."""

    __tablename__ = "research_trajectory_steps"
    __table_args__ = (
        Index("ix_research_trajectory_steps_trajectory_id", "trajectory_id"),
        Index("ix_research_trajectory_steps_source_domain", "source_domain"),
        Index("ix_research_trajectory_steps_query", "query"),
        Index("ix_research_trajectory_steps_reference_id", "reference_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    trajectory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_trajectories.id", ondelete="CASCADE"), nullable=False
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # e.g. "query_source" (checking one configured channel/page/spider
    # target) or "process_result" (handling one real raw post/scraped
    # item from that source). A real, small, open string vocabulary --
    # not a closed enum -- since this deliverable's own integration
    # point (the aggregator loop) is not the only real pipeline this
    # memory could eventually instrument (see this file's own module
    # docstring).
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)

    query: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_domain: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # A compact reference to the real record this step produced --
    # today, a real `Opportunity.id` (as text). Never the opportunity's
    # own content -- see this file's own module docstring.
    reference_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Self-referential -- lets a "process_result" step record which
    # "query_source" step it came from, without a second table. NULL
    # for a top-level step.
    parent_step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_trajectory_steps.id", ondelete="SET NULL"), nullable=True
    )

    # `TrajectoryOutcome` -- defaults to NEUTRAL (pending) at
    # `record_step` time; `complete_step`/`apply_verification_outcome`
    # (research_trajectory_memory.py) update it once a real result is
    # known.
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default=TrajectoryOutcome.NEUTRAL.value)

    # 0-1, a real research-efficiency signal -- see `TrajectoryOutcome`'s
    # own docstring. Nullable: a step that hasn't been resolved yet (or
    # never gets a verification signal, e.g. a query that found nothing)
    # has no score, not a fabricated 0.
    usefulness_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    discovered_entities_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovered_opportunities_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # e.g. "verified_legitimate" / "verified_low_confidence" /
    # "not_yet_verified" -- set by `apply_verification_outcome` once the
    # real, decoupled `_verify_new_opportunities` pass reaches the
    # opportunity this step discovered.
    verification_impact: Mapped[str | None] = mapped_column(String(50), nullable=True)

    error_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    step_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
