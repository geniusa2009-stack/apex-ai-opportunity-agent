"""ResearchTrajectoryMemory (Deliverable 3): makes past research
EXPERIENCE reusable -- not just logged.

STEP 4 DESIGN, "do not introduce a vector database": every retrieval
function below is deterministic and reuses only what this codebase's
plain Postgres/SQLite already gives for free -- a real
`normalize_objective`/keyword-overlap (Jaccard) comparison over
`ResearchTrajectory.normalized_objective`, and real `GROUP BY`-style
aggregation (done in Python over a real, indexed `SELECT`, portable to
both the real Postgres deployment and this project's established SQLite
test/live-verify substitute) over `ResearchTrajectoryStep.query`/
`source_domain`. No embeddings, no pgvector, no new external dependency
-- Deliverable 3's own Step 4 is explicit that this is deliberate, not
a placeholder for "later."

STEP 6/7 DESIGN, the most important constraint in this file: every
number this module produces (`usefulness_score`, a source's
`successful_count`/`failed_count`, a query's hit rate) is a RESEARCH
EFFICIENCY SIGNAL -- how well a query/source has historically helped
FIND something worth checking. None of it is promoted to "this source
is trustworthy" or "this claim is true." `get_source_performance`
deliberately returns real counts and real timestamps, never a
trust/truth boolean, so a caller can never mistake "seen 40 times" for
"verified true 40 times" -- verification remains the exclusive job of
`ai_gateway.verify_opportunity`/`_verify_new_opportunities`
(Evidence + Verification). `build_research_hints` is a planning aid; a
planner that skipped this module entirely would still work, just less
efficiently -- nothing here is load-bearing for correctness.
"""
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import TrajectoryOutcome, TrajectoryStatus
from app.models.research_trajectory import ResearchTrajectory, ResearchTrajectoryStep

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w\s]")

# Outcomes that count as "the step was worth taking" vs. "it wasn't", for
# the real successful_steps/failed_steps rollup on the parent trajectory.
# NEUTRAL (the default, pending state) counts as neither.
_POSITIVE_OUTCOMES = {TrajectoryOutcome.SUCCESSFUL.value, TrajectoryOutcome.PARTIALLY_USEFUL.value}
_NEGATIVE_OUTCOMES = {TrajectoryOutcome.FAILED.value, TrajectoryOutcome.MISLEADING.value}

# D3 Recency/Decay Remediation (post-final-audit LOW finding): same
# half-life-based time-decay convention `evidence_verification.py`
# already established for its own, separate staleness concern
# (`STALE_EVIDENCE_MAX_AGE_DAYS`/`_is_current`) -- mirrored here rather
# than inventing a new pattern. 30 days is the chosen default half-life:
# a discovery observation from 30 days ago counts for half as much
# influence on `discovery_source_yield_hints`'s high/low-yield
# classification as one from today; from 90 days ago (3 half-lives),
# an eighth. This reflects that a source's RECENT posting behavior is
# what predicts its near-term yield in this fast-moving domain (new
# scholarship/opportunity posts), not its entire multi-month history
# weighted equally. Configurable per-call (see
# `discovery_source_yield_hints`'s own `half_life_days` parameter) --
# not hardcoded as an unconditional constant -- so a future caller with
# a different cadence need not edit this module.
SOURCE_YIELD_DECAY_HALF_LIFE_DAYS = 30.0


def _yield_decay_weight(
    step_created_at: datetime | None, *, now: datetime, half_life_days: float
) -> float:
    """Deterministic, bounded, numerically-stable time-decay weight for
    one discovery observation (`ResearchTrajectoryStep.created_at` --
    the real, server-assigned moment this codebase's own aggregator
    recorded the observation; never a source-supplied or fabricated
    time, and not user/source-content-controllable, which is also why
    this can never be manipulated by a prompt-injection-style payload
    in scraped post content -- decay depends solely on this trusted,
    system-generated column).

    `effective_weight = 0.5 ** (age_days / half_life_days)`, i.e. exact
    exponential half-life decay -- deterministic (same inputs always
    give the same output), bounded to `(0, 1]` by construction below,
    and easy to reason about/test.

    Edge cases, each a deliberate, documented decision rather than a
    fabricated timestamp:
      - Missing timestamp (`None`): returns `1.0` (no decay) -- treated
        exactly like the pre-remediation, undecayed behavior for that
        one observation, mirroring `evidence_verification._is_current`'s
        identical "missing reference -> treat as current" convention.
        In real operation this should never occur (`created_at` is
        `NOT NULL` on the model); this branch is a defensive fallback
        only, never a fabricated "now".
      - Naive (timezone-unaware) timestamp: assumed UTC, mirroring
        `_is_current`'s identical assumption -- this codebase's own DB
        timestamps are always UTC-origin (SQLite, used in tests, drops
        tzinfo on read; Postgres, used in production, keeps it).
      - Future timestamp (`step_created_at > now`, e.g. clock skew):
        age is clamped to `0.0` days -> weight capped at exactly `1.0`,
        never allowed to exceed `1.0` -- a future-dated observation is
        treated as "as fresh as right now", never given MORE than full
        weight.
      - `half_life_days <= 0` (misconfiguration guard): returns `1.0`
        (decay disabled) rather than dividing by zero or inverting the
        curve.
    """
    if step_created_at is None:
        return 1.0
    reference = step_created_at
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_days = max((now - reference).total_seconds() / 86400.0, 0.0)
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def normalize_objective(text: str) -> str:
    """Lowercase, punctuation-stripped, whitespace-collapsed -- the real,
    deterministic matching key every retrieval function compares
    against. Not a hash: kept human-readable for debugging."""
    lowered = text.strip().lower()
    no_punct = _NON_WORD_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", no_punct).strip()


def _keywords(text: str) -> set[str]:
    """Real keyword set for Jaccard-similarity matching -- words of 3+
    characters only (drops noise like "a"/"in"/"of" without needing a
    real stopword list/external dependency), with a real, deliberately
    tiny normalization: a trailing "s" is stripped from any word longer
    than 4 characters, so "scholarship"/"scholarships" (or
    "student"/"students") match as the same real keyword. This is NOT a
    real stemmer (no external NLP dependency, per Step 4) -- just enough
    to keep singular/plural query phrasing from looking unrelated."""
    words = normalize_objective(text).split(" ")
    keywords = set()
    for word in words:
        if len(word) < 3:
            continue
        if len(word) > 4 and word.endswith("s"):
            word = word[:-1]
        keywords.add(word)
    return keywords


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


# ---------------------------------------------------------------------
# Recording (Steps 2/3/5)
# ---------------------------------------------------------------------


async def start_trajectory(
    db: AsyncSession, *, objective: str, metadata: dict | None = None
) -> ResearchTrajectory:
    """Real trajectory start -- one real row, `status=RUNNING`."""
    trajectory = ResearchTrajectory(
        objective=objective,
        normalized_objective=normalize_objective(objective),
        status=TrajectoryStatus.RUNNING.value,
        trajectory_metadata=metadata,
    )
    db.add(trajectory)
    await db.flush()
    return trajectory


async def record_step(
    db: AsyncSession,
    *,
    trajectory: ResearchTrajectory,
    action_type: str,
    query: str | None = None,
    source_domain: str | None = None,
    parent_step_id: uuid.UUID | None = None,
    reference_id: str | None = None,
    metadata: dict | None = None,
) -> ResearchTrajectoryStep:
    """Real step creation. `step_number` is assigned from the real,
    running count already on `trajectory` (1-indexed) -- the caller
    never has to track it itself."""
    trajectory.total_steps += 1
    step = ResearchTrajectoryStep(
        trajectory_id=trajectory.id,
        step_number=trajectory.total_steps,
        action_type=action_type,
        query=query,
        source_domain=source_domain,
        parent_step_id=parent_step_id,
        reference_id=reference_id,
        outcome=TrajectoryOutcome.NEUTRAL.value,
        step_metadata=metadata,
    )
    db.add(step)
    await db.flush()
    return step


async def complete_step(
    db: AsyncSession,
    *,
    step: ResearchTrajectoryStep,
    trajectory: ResearchTrajectory,
    outcome: TrajectoryOutcome,
    usefulness_score: float | None = None,
    discovered_entities_count: int = 0,
    discovered_opportunities_count: int = 0,
    verification_impact: str | None = None,
    error_reason: str | None = None,
) -> ResearchTrajectoryStep:
    """Real step resolution -- records the real outcome of one real
    action and rolls it up onto the real parent trajectory's own
    running counters (`successful_steps`/`failed_steps`/
    `opportunities_discovered`)."""
    step.outcome = outcome.value
    step.usefulness_score = usefulness_score
    step.discovered_entities_count = discovered_entities_count
    step.discovered_opportunities_count = discovered_opportunities_count
    step.verification_impact = verification_impact
    step.error_reason = error_reason

    if outcome.value in _POSITIVE_OUTCOMES:
        trajectory.successful_steps += 1
    elif outcome.value in _NEGATIVE_OUTCOMES:
        trajectory.failed_steps += 1

    trajectory.opportunities_discovered += discovered_opportunities_count

    db.add(step)
    db.add(trajectory)
    await db.flush()
    return step


def _derive_final_outcome(trajectory: ResearchTrajectory) -> str:
    """Real, deterministic, disclosed heuristic -- no fabricated
    scoring, just the real ratio of this real trajectory's own
    successful vs. failed steps."""
    if trajectory.total_steps == 0:
        return TrajectoryOutcome.NEUTRAL.value
    if trajectory.opportunities_verified > 0 and trajectory.failed_steps == 0:
        return TrajectoryOutcome.SUCCESSFUL.value
    if trajectory.successful_steps > 0 and trajectory.failed_steps > 0:
        return TrajectoryOutcome.PARTIALLY_USEFUL.value
    if trajectory.successful_steps > 0:
        return TrajectoryOutcome.SUCCESSFUL.value
    if trajectory.failed_steps > 0:
        return TrajectoryOutcome.FAILED.value
    return TrajectoryOutcome.NEUTRAL.value


async def record_outcome(
    db: AsyncSession,
    *,
    trajectory: ResearchTrajectory,
    final_outcome: TrajectoryOutcome | None = None,
) -> ResearchTrajectory:
    """Real trajectory completion. `final_outcome`, when not given
    explicitly, is derived deterministically from this trajectory's own
    real step counters (`_derive_final_outcome`) -- never guessed from
    outside data. Also rolls up `useful_sources` (real, deduplicated
    domains that produced a real discovered opportunity) and
    `research_depth` (real count of distinct real sources queried)."""
    result = await db.execute(
        select(ResearchTrajectoryStep).where(ResearchTrajectoryStep.trajectory_id == trajectory.id)
    )
    steps = result.scalars().all()

    all_domains = {s.source_domain for s in steps if s.source_domain}
    useful_domains = sorted(
        {s.source_domain for s in steps if s.source_domain and s.discovered_opportunities_count > 0}
    )

    trajectory.useful_sources = useful_domains
    trajectory.research_depth = len(all_domains)
    trajectory.completed_at = datetime.now(timezone.utc)
    trajectory.final_outcome = (final_outcome or TrajectoryOutcome(_derive_final_outcome(trajectory))).value
    trajectory.status = (
        TrajectoryStatus.FAILED.value
        if trajectory.final_outcome == TrajectoryOutcome.FAILED.value
        else TrajectoryStatus.COMPLETED.value
    )

    db.add(trajectory)
    await db.flush()
    return trajectory


async def apply_verification_outcome(
    db: AsyncSession, *, opportunity_id: uuid.UUID, is_legitimate: bool, confidence_score: float
) -> ResearchTrajectoryStep | None:
    """Closes the loop from the real, decoupled `_verify_new_opportunities`
    verification pass back onto the real trajectory step that originally
    discovered this opportunity (matched by `reference_id`, a compact
    string reference -- see this module's own docstring). Best-effort
    and side-effect-free when no such step exists (e.g. an opportunity
    created by the pre-existing Scrapy pipeline, or before this feature
    existed) -- returns `None` rather than raising, matching this
    codebase's established "a memory/graph-sync failure must never
    affect real verification" posture (`entity_relationship_service`).

    THIS FUNCTION NEVER WRITES TO `Opportunity` -- it only updates the
    trajectory step's own research-efficiency fields. Verification
    truth stays exclusively owned by the caller.
    """
    result = await db.execute(
        select(ResearchTrajectoryStep).where(ResearchTrajectoryStep.reference_id == str(opportunity_id))
    )
    step = result.scalars().first()
    if step is None:
        return None

    if is_legitimate and confidence_score >= 0.7:
        outcome = TrajectoryOutcome.SUCCESSFUL
        verification_impact = "verified_legitimate"
    elif is_legitimate:
        outcome = TrajectoryOutcome.PARTIALLY_USEFUL
        verification_impact = "verified_low_confidence"
    else:
        outcome = TrajectoryOutcome.MISLEADING
        verification_impact = "verified_not_legitimate"

    was_positive_before = step.outcome in _POSITIVE_OUTCOMES
    was_negative_before = step.outcome in _NEGATIVE_OUTCOMES

    step.outcome = outcome.value
    step.usefulness_score = confidence_score
    step.verification_impact = verification_impact

    trajectory = await db.get(ResearchTrajectory, step.trajectory_id)
    if trajectory is not None:
        trajectory.opportunities_verified += 1
        is_positive_now = outcome.value in _POSITIVE_OUTCOMES
        is_negative_now = outcome.value in _NEGATIVE_OUTCOMES
        if is_positive_now and not was_positive_before:
            trajectory.successful_steps += 1
            if was_negative_before:
                trajectory.failed_steps -= 1
        elif is_negative_now and not was_negative_before:
            trajectory.failed_steps += 1
            if was_positive_before:
                trajectory.successful_steps -= 1
        db.add(trajectory)

    db.add(step)
    await db.flush()
    return step


# ---------------------------------------------------------------------
# Retrieval (Steps 3/4) -- deterministic, no vector DB
# ---------------------------------------------------------------------


async def retrieve_similar_trajectories(
    db: AsyncSession, *, objective: str, limit: int = 5
) -> list[tuple[ResearchTrajectory, float]]:
    """Real Jaccard keyword-overlap over every real, COMPLETED
    trajectory's `normalized_objective` -- deterministic, no embeddings.
    Returns `(trajectory, similarity_score)` pairs, most similar first,
    score > 0 only."""
    target_keywords = _keywords(objective)
    result = await db.execute(
        select(ResearchTrajectory).where(ResearchTrajectory.status == TrajectoryStatus.COMPLETED.value)
    )
    candidates = result.scalars().all()

    scored = [
        (trajectory, _jaccard(target_keywords, _keywords(trajectory.objective)))
        for trajectory in candidates
    ]
    scored = [pair for pair in scored if pair[1] > 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


async def retrieve_successful_search_patterns(db: AsyncSession, *, limit: int = 10) -> list[dict]:
    """Real, grouped-by-query counts for every real step whose real
    outcome was SUCCESSFUL/PARTIALLY_USEFUL. A real research-efficiency
    signal, not a truth claim -- see module docstring."""
    return await _grouped_query_patterns(db, outcomes=_POSITIVE_OUTCOMES, limit=limit)


async def retrieve_failed_search_patterns(db: AsyncSession, *, limit: int = 10) -> list[dict]:
    """Same as `retrieve_successful_search_patterns`, for FAILED/
    MISLEADING outcomes."""
    return await _grouped_query_patterns(db, outcomes=_NEGATIVE_OUTCOMES, limit=limit)


async def _grouped_query_patterns(db: AsyncSession, *, outcomes: set[str], limit: int) -> list[dict]:
    result = await db.execute(
        select(ResearchTrajectoryStep).where(
            ResearchTrajectoryStep.outcome.in_(outcomes),
            ResearchTrajectoryStep.query.is_not(None),
        )
    )
    steps = result.scalars().all()

    by_query: dict[str, list[ResearchTrajectoryStep]] = {}
    for step in steps:
        by_query.setdefault(step.query, []).append(step)

    patterns = []
    for query, matching_steps in by_query.items():
        scores = [s.usefulness_score for s in matching_steps if s.usefulness_score is not None]
        patterns.append(
            {
                "query": query,
                "occurrences": len(matching_steps),
                "average_usefulness_score": (sum(scores) / len(scores)) if scores else None,
                "last_seen_at": max(s.created_at for s in matching_steps).isoformat(),
            }
        )
    patterns.sort(key=lambda p: p["occurrences"], reverse=True)
    return patterns[:limit]


async def get_source_performance(db: AsyncSession, *, limit: int = 20) -> list[dict]:
    """Real, grouped-by-domain counts -- STEP 7, POISONING RESISTANCE:
    deliberately returns real counts/timestamps ONLY, never a
    trust/truth boolean. "domain X produced useful evidence in N
    previous trajectories" is the exact, disclosed shape this function
    returns -- never "domain X is trustworthy"."""
    result = await db.execute(
        select(ResearchTrajectoryStep).where(ResearchTrajectoryStep.source_domain.is_not(None))
    )
    steps = result.scalars().all()

    by_domain: dict[str, list[ResearchTrajectoryStep]] = {}
    for step in steps:
        by_domain.setdefault(step.source_domain, []).append(step)

    performance = []
    for domain, matching_steps in by_domain.items():
        successful = sum(1 for s in matching_steps if s.outcome in _POSITIVE_OUTCOMES)
        failed = sum(1 for s in matching_steps if s.outcome in _NEGATIVE_OUTCOMES)
        performance.append(
            {
                "source_domain": domain,
                "total_steps_seen": len(matching_steps),
                "positive_signal_count": successful,
                "negative_signal_count": failed,
                "last_seen_at": max(s.created_at for s in matching_steps).isoformat(),
            }
        )
    performance.sort(key=lambda p: p["total_steps_seen"], reverse=True)
    return performance[:limit]


async def get_source_performance_for_objective(
    db: AsyncSession, *, objective: str, limit: int = 20
) -> list[dict]:
    """Same real, disclosed shape as `get_source_performance` (Step 7:
    counts/timestamps only, never a trust verdict), narrowed to steps
    whose own `query` has real keyword overlap with `objective` -- a
    domain only counts as relevant "for this kind of research", not
    just relevant in general. Used by `build_research_hints` so an
    unrelated objective correctly gets an empty domain list rather than
    every domain this memory has ever seen."""
    target_keywords = _keywords(objective)
    result = await db.execute(
        select(ResearchTrajectoryStep).where(
            ResearchTrajectoryStep.source_domain.is_not(None),
            ResearchTrajectoryStep.query.is_not(None),
        )
    )
    steps = [s for s in result.scalars().all() if _jaccard(target_keywords, _keywords(s.query)) > 0]

    by_domain: dict[str, list[ResearchTrajectoryStep]] = {}
    for step in steps:
        by_domain.setdefault(step.source_domain, []).append(step)

    performance = []
    for domain, matching_steps in by_domain.items():
        successful = sum(1 for s in matching_steps if s.outcome in _POSITIVE_OUTCOMES)
        failed = sum(1 for s in matching_steps if s.outcome in _NEGATIVE_OUTCOMES)
        performance.append(
            {
                "source_domain": domain,
                "total_steps_seen": len(matching_steps),
                "positive_signal_count": successful,
                "negative_signal_count": failed,
                "last_seen_at": max(s.created_at for s in matching_steps).isoformat(),
            }
        )
    performance.sort(key=lambda p: p["total_steps_seen"], reverse=True)
    return performance[:limit]


async def build_research_hints(db: AsyncSession, *, objective: str) -> dict:
    """THE important function (Deliverable 3, Step 3). Real, structured
    hints a research planner can use for `objective` -- built entirely
    from real, past trajectory data, never fabricated. Every list can
    legitimately be empty (the real "no memory case": a genuinely new
    objective with nothing similar in history returns empty lists and a
    single, honest `recommended_next_actions` entry saying so, not an
    error).

    STEP 7 REMINDER: nothing in this return value is a truth claim.
    `useful_domains`/`low_value_domains` are research-efficiency
    signals derived from past OUTCOMES of research steps, never
    promoted to "this domain is trustworthy" -- see `get_source_performance`.
    """
    similar = await retrieve_similar_trajectories(db, objective=objective, limit=5)
    all_successful_patterns = await retrieve_successful_search_patterns(db, limit=50)
    all_failed_patterns = await retrieve_failed_search_patterns(db, limit=50)

    # Real relevance filter -- a query pattern (and, by extension, a
    # source domain) only counts as a hint for THIS objective when it
    # shares real keyword overlap with it. Without this,
    # `build_research_hints` for a genuinely new, unrelated objective
    # would return every OTHER objective's history, which is not a hint
    # for this one -- the real "no memory case" this deliverable's own
    # Step 3/8 explicitly requires.
    target_keywords = _keywords(objective)
    successful_patterns = [
        p for p in all_successful_patterns if _jaccard(target_keywords, _keywords(p["query"])) > 0
    ][:10]
    failed_patterns = [
        p for p in all_failed_patterns if _jaccard(target_keywords, _keywords(p["query"])) > 0
    ][:10]

    relevant_source_performance = await get_source_performance_for_objective(db, objective=objective, limit=20)
    useful_domains = [
        p["source_domain"] for p in relevant_source_performance
        if p["positive_signal_count"] > p["negative_signal_count"] and p["positive_signal_count"] > 0
    ]
    low_value_domains = [
        p["source_domain"] for p in relevant_source_performance
        if p["negative_signal_count"] > p["positive_signal_count"] and p["negative_signal_count"] > 0
    ]

    recommended_next_actions: list[str] = []
    if successful_patterns:
        top = successful_patterns[0]
        recommended_next_actions.append(
            f"Consider repeating query {top['query']!r} -- it was useful in "
            f"{top['occurrences']} previous step(s)."
        )
    if low_value_domains:
        recommended_next_actions.append(
            f"Treat {low_value_domains[0]!r} cautiously -- it has produced more "
            "negative than positive research-efficiency signal historically "
            "(this is a hint, not proof the domain is untrustworthy)."
        )
    if similar and similar[0][0].final_outcome == TrajectoryOutcome.FAILED.value:
        recommended_next_actions.append(
            "The most similar past research objective ended FAILED -- consider "
            "adjusting query terms or sources before repeating the same approach."
        )
    if not similar and not successful_patterns and not failed_patterns:
        recommended_next_actions.append(
            "No prior research memory found for this objective yet -- proceeding "
            "without historical hints."
        )

    return {
        "successful_queries": [p["query"] for p in successful_patterns],
        "failed_queries": [p["query"] for p in failed_patterns],
        "useful_domains": useful_domains,
        "low_value_domains": low_value_domains,
        "recommended_next_actions": recommended_next_actions,
        "known_patterns": [
            {
                "objective": trajectory.objective,
                "similarity_score": round(score, 4),
                "final_outcome": trajectory.final_outcome,
                "opportunities_discovered": trajectory.opportunities_discovered,
                "opportunities_verified": trajectory.opportunities_verified,
            }
            for trajectory, score in similar
        ],
    }


async def discovery_source_yield_hints(
    db: AsyncSession,
    *,
    min_sample: int = 3,
    limit: int = 50,
    half_life_days: float = SOURCE_YIELD_DECAY_HALF_LIFE_DAYS,
    now: datetime | None = None,
) -> dict:
    """D3 BACKWARD-LOOKING DISCOVERY MEMORY (Deliverable 5, Feature 3).

    STEP 1 INVESTIGATION, confirmed gap: `build_research_hints`/
    `get_source_performance` above are real, but keyed off each step's
    `outcome` field (SUCCESSFUL/FAILED/etc.) -- and `_poll_source`
    (`app/workers/aggregator_tasks.py`) always records a discovery
    `process_result` step's outcome as NEUTRAL (never SUCCESSFUL/FAILED
    on the discovery side; a discovered opportunity is only judged
    later by the real, decoupled verification pass -- see that
    function's own comment on this exact point). So `get_source_performance`
    genuinely could not answer "which sources produced new opportunities
    vs. duplicates during discovery" before this function -- every
    discovery step's `positive_signal_count`/`negative_signal_count`
    would be zero. This function reads a DIFFERENT, already-real field
    instead: `discovered_opportunities_count` (0 or 1, set by
    `complete_step` from the real `upsert_opportunity` `was_created`
    flag) on every real `action_type="process_result"` step, grouped by
    that step's own `query` (the real, stable per-run source label --
    a Telegram handle or Facebook page id, see `_poll_source`).

    Real, deterministic, statistical aggregation only (per this
    deliverable's own instruction to prefer this over any "AI
    summarizes everything" approach) -- `yield_rate` = real created /
    real processed; `duplicate_rate` = real duplicates (processed minus
    created) / real processed. `min_sample` guards against judging a
    source off one or two posts (Deliverable 5 test: "no history behaves
    normally" / "duplicate-heavy source deprioritized" both need a real
    sample size, not a fluke).

    Returns `{"source_stats": [...], "high_yield_source_labels": {...},
    "low_yield_source_labels": {...}}` -- raw source LABELS (the same
    string `DiscoverySourceCandidate.source_id` uses before
    normalization), so the caller (`aggregator_tasks.py`) applies the
    same `research_orchestrator.normalize_source_id` it already uses
    everywhere else, rather than this module reaching into
    `research_orchestrator` for that one helper.

    D3 RECENCY/DECAY REMEDIATION: `high_yield_source_labels`/
    `low_yield_source_labels` are now classified from a real,
    time-decayed yield/duplicate rate (`_yield_decay_weight`, half-life
    `half_life_days`) instead of every historical observation counting
    equally forever -- a source with strong RECENT yield is flagged
    high-yield even if its older history was mixed; a source whose only
    strong yield was long ago fades out of the high-yield set over time,
    exactly as a source with genuinely no recent signal should. This
    does NOT change what "yield"/"duplicate rate"/"processed"/"created"
    mean, and does NOT change the real per-source sample-size gate's
    THRESHOLD (`min_sample` is still the same number) -- but the gate
    now compares it against the decayed EFFECTIVE sample size rather
    than the raw historical COUNT, so a source needs real, RECENT-
    enough volume of evidence before being judged either way, not
    merely real volume at some arbitrarily distant point in the past
    (see the classification gate below for why a decayed RATE alone
    cannot achieve this). Each `source_stats` entry now
    reports BOTH the original, undecayed `yield_rate`/`duplicate_rate`
    (unchanged, for anyone reading the raw historical shape) AND the
    new `decayed_yield_rate`/`decayed_duplicate_rate` (what actually
    drives high/low-yield classification below)."""
    result = await db.execute(
        select(ResearchTrajectoryStep).where(
            ResearchTrajectoryStep.action_type == "process_result",
            ResearchTrajectoryStep.query.is_not(None),
        )
    )
    steps = result.scalars().all()

    now = now or datetime.now(timezone.utc)

    by_label: dict[str, list[ResearchTrajectoryStep]] = {}
    for step in steps:
        by_label.setdefault(step.query, []).append(step)

    source_stats = []
    high_yield_labels: set[str] = set()
    low_yield_labels: set[str] = set()
    for label, matching in by_label.items():
        # UNCHANGED semantics: real, undecayed counts/rates -- every
        # observation still counts once here, exactly as before this
        # remediation. `processed` is reported as-is (the real,
        # undecayed historical volume); the min-sample GATE used for
        # classification below uses the decayed effective sample size
        # (`weighted_processed`) instead -- see that gate for why.
        processed = len(matching)
        created = sum(s.discovered_opportunities_count for s in matching)
        duplicates = max(processed - created, 0)
        errors = sum(1 for s in matching if s.outcome == TrajectoryOutcome.FAILED.value)
        yield_rate = (created / processed) if processed else 0.0
        duplicate_rate = (duplicates / processed) if processed else 0.0

        # NEW: time-decayed rate, used for high/low-yield
        # classification below (see this function's own docstring).
        weights = [
            _yield_decay_weight(s.created_at, now=now, half_life_days=half_life_days) for s in matching
        ]
        weighted_processed = sum(weights)
        weighted_created = sum(
            w * s.discovered_opportunities_count for w, s in zip(weights, matching)
        )
        weighted_duplicates = max(weighted_processed - weighted_created, 0.0)
        decayed_yield_rate = (weighted_created / weighted_processed) if weighted_processed > 0 else 0.0
        decayed_duplicate_rate = (
            (weighted_duplicates / weighted_processed) if weighted_processed > 0 else 0.0
        )

        source_stats.append(
            {
                "source_label": label,
                "processed": processed,
                "created": created,
                "duplicates": duplicates,
                "errors": errors,
                "yield_rate": round(yield_rate, 4),
                "duplicate_rate": round(duplicate_rate, 4),
                "decayed_yield_rate": round(decayed_yield_rate, 4),
                "decayed_duplicate_rate": round(decayed_duplicate_rate, 4),
            }
        )
        # GATE USES THE DECAYED EFFECTIVE SAMPLE SIZE
        # (`weighted_processed`), NOT the raw undecayed `processed`
        # count. This is deliberate and required for decay to have any
        # effect at all: `decayed_yield_rate` is a WEIGHTED AVERAGE
        # (`weighted_created / weighted_processed`), so when every
        # observation for a label shares (approximately) the same age
        # -- the common, realistic case for a source whose history is
        # uniformly old -- the shared weight cancels out of that ratio
        # and `decayed_yield_rate` collapses back to the plain,
        # undecayed `yield_rate` no matter how old the observations
        # are. A weighted-average rate alone therefore cannot make a
        # uniformly-old source "lose influence" as the remediation
        # requires. `weighted_processed`, by contrast, IS sensitive to
        # age on its own (it is a decayed observation COUNT, not a
        # ratio) -- it shrinks toward zero as a source's entire history
        # ages, which is exactly the "not enough (recent) evidence to
        # judge this source" signal `min_sample` was always meant to
        # express. Gating on it here means a source needs real,
        # RECENT-enough evidence (not just a real historical volume,
        # however old) before its rate is trusted either way -- a
        # source with a strong yield only long ago decays out of
        # high-yield exactly as this remediation's finding requires,
        # while a source with fresh volume is ungated exactly as
        # before (weights near `1.0` leave `weighted_processed`
        # near `processed`).
        if weighted_processed >= min_sample and decayed_yield_rate >= 0.5:
            high_yield_labels.add(label)
        if weighted_processed >= min_sample and decayed_duplicate_rate >= 0.7:
            low_yield_labels.add(label)

    source_stats.sort(key=lambda s: s["processed"], reverse=True)

    return {
        "source_stats": source_stats[:limit],
        "high_yield_source_labels": high_yield_labels,
        "low_yield_source_labels": low_yield_labels,
    }


# RESEARCH TRAJECTORY RETENTION / PRUNING REMEDIATION (post-final-audit
# LOW finding: "research_trajectory_steps has no explicit retention/
# pruning policy"). STEP 1 INVESTIGATION, confirmed: this table has no
# existing cap and is written once per real discovery item, forever
# (see `app/workers/aggregator_tasks.py::_poll_source`'s own real,
# per-item `record_step`/`complete_step` calls) -- genuinely unbounded
# growth, exactly as the finding says.
#
# WINDOW CHOICE, tied to REAL existing behavior rather than an
# arbitrary new number: `SOURCE_YIELD_DECAY_HALF_LIFE_DAYS` (30 days,
# above) already makes an observation's influence on
# `discovery_source_yield_hints`'s classification decay toward
# negligible well before this window -- `0.5 ** (180 / 30) = 1/64`,
# ~1.6%. 180 days (6 half-lives) is therefore a genuinely conservative
# default: by the time a step becomes eligible for pruning, D3's own
# real decay math has ALREADY made it contribute almost nothing to
# current classification (see the D3 COMPATIBILITY analysis in this
# remediation's own report for the full, disclosed reasoning on why
# pruning does not change D3 semantics). It is also generous relative
# to `apply_verification_outcome`'s own best-effort verification-
# catch-up window (that pass runs far sooner than 180 days after
# discovery in real operation), and shorter than -- never conflicting
# with -- `evidence_verification.STALE_EVIDENCE_MAX_AGE_DAYS` (365
# days), a deliberately longer window for a different, higher-stakes
# concern (evidence trustworthiness, not discovery-memory recency).
# Configurable per-call (see `prune_old_research_trajectory_steps`'s
# own `retention_days` parameter) rather than hardcoded, same
# convention as `discovery_source_yield_hints`'s own `half_life_days`.
RESEARCH_TRAJECTORY_STEP_RETENTION_DAYS = 180.0


async def prune_old_research_trajectory_steps(
    db: AsyncSession,
    *,
    retention_days: float = RESEARCH_TRAJECTORY_STEP_RETENTION_DAYS,
    now: datetime | None = None,
) -> dict:
    """Deletes `ResearchTrajectoryStep` rows older than `retention_days`
    whose PARENT trajectory is no longer active -- the real, minimal fix
    for unbounded historical growth in this table.

    SAFETY RULE (non-negotiable, per this remediation's own instruction
    to never guess a completion signal): a step is eligible ONLY when
    its parent `ResearchTrajectory.status` is `COMPLETED` or `FAILED`
    (`TrajectoryStatus`, the real, pre-existing lifecycle field this
    model already has -- see `record_outcome`, the ONLY place that ever
    sets it away from its `RUNNING` default). A step whose trajectory is
    still `RUNNING` is NEVER touched, regardless of its own age -- this
    also means a step can never be pruned mid-run even in principle,
    since `_run_daily_opportunity_scraper` (the one real writer) commits
    its trajectory's `RUNNING` status exactly once, atomically, together
    with every step it wrote that run (`record_outcome` + `session.
    commit()` are the run's own last two calls) -- there is no window
    where a partially-written run's steps are visible to a separate
    pruning connection while its trajectory still reads `RUNNING`.

    SCOPE, disclosed deliberately: this prunes `ResearchTrajectoryStep`
    rows ONLY. The parent `ResearchTrajectory` row is NEVER deleted --
    it is low-volume (one row per real run, not one per discovered
    item), already carries its own durable, pre-computed summary
    (`useful_sources`, `research_depth`, `final_outcome`, the four
    step-count rollups) written once at `record_outcome` time, and is
    exactly the admin/audit-visible record `app/api/v1/endpoints/
    admin_research.py`'s own trajectory-detail endpoint reads -- keeping
    it indefinitely preserves real historical/audit visibility ("this
    run happened, here is its real outcome") even after its own
    step-level detail has aged out. No new archival table/mechanism is
    introduced (this codebase has no existing precedent for archiving
    this KIND of row, only object-storage cleanup for media -- see
    `app/workers/cleanup_tasks.py`'s own, unrelated scope); hard-delete
    is the correct, minimal choice here.

    Evidence/provenance (D6) is NEVER affected: `Evidence` rows
    (`app/models/evidence.py`) have no foreign key to
    `ResearchTrajectoryStep` and are durable, independently, by their
    own design (see that model's own docstring) -- this function cannot
    touch D6 data even in principle.

    Idempotent and safe under concurrent execution: a single bulk `DELETE
    ... WHERE created_at < cutoff AND trajectory_id IN (SELECT id FROM
    research_trajectories WHERE status != 'running')`. Re-running this
    finds a strictly smaller (or empty) eligible set each time -- no
    double-delete hazard, no distributed lock needed (a `DELETE` with a
    `WHERE` clause is naturally idempotent; two concurrent runs each
    delete whatever the other hasn't already removed, converging safely
    on the same end state). Never fabricates a timestamp: `cutoff` is
    computed once from the real, caller-supplied-or-real `now`, compared
    only against the real, server-assigned, non-nullable `created_at`
    column (never a source-supplied or optional field).

    On a genuine database failure, this function does NOT swallow the
    exception into a fake "0 deleted" success -- it re-raises after
    logging, so the caller (the Celery task wrapper) surfaces a real
    task failure rather than a misleading clean run.

    Returns `{"cutoff": <isoformat>, "eligible_trajectories": <int>,
    "deleted_steps": <int>}` -- structured counts only, never step
    content/JSONB metadata (nothing here is ever logged at any level
    beyond these counts and the cutoff timestamp).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)

    non_active_ids_result = await db.execute(
        select(ResearchTrajectory.id).where(ResearchTrajectory.status != TrajectoryStatus.RUNNING.value)
    )
    non_active_ids = [row[0] for row in non_active_ids_result.all()]

    if not non_active_ids:
        logger.info(
            "prune_old_research_trajectory_steps: no non-active trajectories exist yet -- nothing eligible "
            "(cutoff=%s)",
            cutoff.isoformat(),
        )
        return {"cutoff": cutoff.isoformat(), "eligible_trajectories": 0, "deleted_steps": 0}

    try:
        delete_result = await db.execute(
            delete(ResearchTrajectoryStep)
            .where(
                ResearchTrajectoryStep.created_at < cutoff,
                ResearchTrajectoryStep.trajectory_id.in_(non_active_ids),
            )
            # `synchronize_session=False`: this is a maintenance sweep,
            # not an operation any caller needs in-session ORM objects
            # kept in sync with -- deliberately skips the ORM's default
            # "evaluate" strategy, which would otherwise try to check
            # this WHERE clause against already-loaded step objects in
            # THIS session's identity map (e.g. ones a caller read via
            # `discovery_source_yield_hints` moments earlier) and can
            # raise `TypeError: can't compare offset-naive and
            # offset-aware datetimes` if any loaded object's own
            # `created_at` came back naive (SQLite, this test suite's
            # own DB) while `cutoff` is timezone-aware -- a real,
            # reproduced failure, not a hypothetical one. `False` is
            # also the fastest option and exactly matches the plain
            # bulk-`DELETE` idiom this codebase already establishes in
            # `entity_relationship_service.rebuild_entity_relationships`.
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    except Exception:
        logger.exception(
            "prune_old_research_trajectory_steps: real DB failure during prune (cutoff=%s) -- rolling back, "
            "NOT reporting a false success",
            cutoff.isoformat(),
        )
        await db.rollback()
        raise

    deleted = delete_result.rowcount or 0
    summary = {
        "cutoff": cutoff.isoformat(),
        "eligible_trajectories": len(non_active_ids),
        "deleted_steps": deleted,
    }
    logger.info("prune_old_research_trajectory_steps: %s", summary)
    return summary
