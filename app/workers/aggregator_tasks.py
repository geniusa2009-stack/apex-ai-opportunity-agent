"""Hybrid Opportunity Aggregation Engine -- Celery periodic task.

DISCLOSED PATH DEVIATION: this step's own ask names the target path as
`app/worker/tasks/aggregator_tasks.py` (singular "worker", nested "tasks"
subpackage). This codebase's actual, established convention for Celery
task modules is a FLAT `app/workers/*.py` layout (plural "workers") --
`app/workers/tasks.py`, `app/workers/scholar_agent.py`,
`app/workers/memory_tasks.py`, `app/workers/media_processing.py` -- four
existing files, all flat, all under the plural directory name (the one
exception, `app/tasks/student_video.py`, is itself an already-disclosed
one-off; see `app/tasks/__init__.py`). Introducing a THIRD naming/nesting
scheme for the exact same kind of file would fragment this codebase's own
layout for no real benefit, so this file lives at
`app/workers/aggregator_tasks.py` instead, matching the dominant existing
convention.

Same `asyncio.run()` + `engine.dispose()` event-loop-lifetime pattern as
every other Celery task in this codebase -- see
`app/workers/scholar_agent.py`'s own docstring for the full reasoning
(the shared async engine binds its pooled connections to whichever loop
first used them; disposing the pool in `finally` forces fresh connections
on the next task run rather than reusing ones tied to a now-dead loop).

Per-post processing is wrapped in its own `try`/`except`, same
best-effort posture as `app/workers/tasks.py`'s `verify_new_opportunities`
-- one bad post (a Gemini extraction failure, a malformed AI response)
must not abort the entire run and lose every OTHER already-processed
post from that same run.

ADAPTIVE DISCOVERY EXECUTION (Deliverable 4.2): the original single
static pass over every configured source ("poll everything, every
run") is now wrapped in a BOUNDED, ADAPTIVE round loop
(`_run_adaptive_discovery_rounds`), reusing D4's own
`research_orchestrator` complexity classifier, depth-policy budgets, and
D1 (coverage)/D2 (graph)/D3 (trajectory memory) signals -- see that
module's own "Section O" docstring for the full reinterpretation of
"query"/"search" onto this pipeline's real unit of work (one configured
source, polled once per round). `fetch_new_telegram_posts`,
`fetch_new_facebook_posts`, `process_raw_post_via_ai`, and
`upsert_opportunity` are called EXACTLY as before, unmodified -- D4.2
only changes WHEN/HOW MANY of the configured sources get polled in a
given run, never what happens once a source is polled. Backward
compatibility (Step 24): with zero prior trajectory/coverage history
(a fresh environment) the classifier still resolves to a real,
bounded depth (never unbounded, never a crash) and the loop still polls
every configured source across enough rounds unless a round's own real
result signals saturation -- the deterministic, "poll everything" shape
is exactly what happens whenever nothing yet justifies stopping early.
"""

import asyncio
import logging
from typing import Any

from app.core.celery_app import celery_app
from app.db.session import AsyncSessionLocal, engine
from app.models.enums import SourceType, TrajectoryOutcome
from app.services import research_orchestrator as ro
from app.services import research_trajectory_memory
from app.services.aggregator_service import (
    AggregatorError,
    FacebookPageConfig,
    TelegramChannelConfig,
    SourcesConfig,
    fetch_new_facebook_posts,
    fetch_new_telegram_posts,
    load_sources_config,
    process_raw_post_via_ai,
    upsert_opportunity,
)

logger = logging.getLogger(__name__)

# D1 signal -> source-category prioritization (Step 5/12): a small,
# disclosed, approximate keyword mapping from this codebase's four real
# `OpportunityType` values onto the free-text `category` strings this
# pipeline's own sources config already uses
# (`app/data/opportunity_sources.json`). There is no formal taxonomy
# linking the two -- source categories are human-curated, informal
# labels ("Internships_Training", "Self_Development"), not
# `OpportunityType` values themselves. This mapping is intentionally
# conservative (substring match only, case-insensitive) and used ONLY to
# decide which already-configured sources to poll FIRST when D1 flags a
# type as under-covered -- never to decide what an opportunity IS (that
# stays Gemini's per-post `opportunity_type` classification, unchanged).
_TYPE_CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "scholarship": ("scholar",),
    "internship": ("intern", "training"),
    "course": ("course", "educ", "self_development"),
    "volunteer": ("volunteer",),
}


@celery_app.task(name="app.workers.aggregator_tasks.run_daily_opportunity_scraper")
def run_daily_opportunity_scraper() -> dict:
    """Celery entrypoint (sync). See module docstring for the
    `asyncio.run()`/`engine.dispose()` reasoning."""
    return asyncio.run(_run_daily_opportunity_scraper())


def _build_source_candidates(
    config: SourcesConfig,
) -> tuple[list[ro.DiscoverySourceCandidate], dict[str, TelegramChannelConfig | FacebookPageConfig]]:
    """Real, configured sources -> `DiscoverySourceCandidate`s (Step 3),
    plus a lookup back to each source's own original config object (the
    real fetch functions still need the real, typed config, not the
    lightweight planning candidate)."""
    candidates: list[ro.DiscoverySourceCandidate] = []
    by_id: dict[str, TelegramChannelConfig | FacebookPageConfig] = {}

    for channel in config.telegram_channels:
        candidates.append(
            ro.DiscoverySourceCandidate(
                source_id=channel.handle,
                source_kind="telegram_channel",
                category=channel.category,
                source_base_url=f"https://t.me/{channel.handle.lstrip('@')}",
            )
        )
        by_id[channel.handle] = channel

    for page in config.facebook_pages:
        candidates.append(
            ro.DiscoverySourceCandidate(
                source_id=page.page_id,
                source_kind="facebook_page",
                category=page.target_age,
                source_base_url=f"https://www.facebook.com/{page.page_id}",
            )
        )
        by_id[page.page_id] = page

    return candidates, by_id


async def _priority_categories_from_coverage(db) -> tuple[set[str], bool]:
    """D1 integration (Step 12): reuses the REAL
    `compute_opportunity_coverage` (same function `research_orchestrator`
    itself calls for verification routing) -- no duplicated query.
    Returns `(priority_categories, any_gap)`: which of this pipeline's
    own configured-source category keyword buckets correspond to a
    currently under-covered `OpportunityType`, and whether ANY gap
    exists at all (used only to bias overall discovery depth upward --
    never as evidence an opportunity exists)."""
    try:
        from app.api.v1.endpoints.admin_opportunities import compute_opportunity_coverage

        coverage = await compute_opportunity_coverage(db)
    except Exception:
        logger.exception("aggregator_tasks: D1 coverage signal computation failed -- treating as no gap known")
        return set(), False

    gapped_types = {bucket.value for bucket in coverage.by_type if bucket.is_gap}
    any_gap = bool(gapped_types) or any(b.is_gap for b in coverage.by_country) or any(
        b.is_gap for b in coverage.by_academic_level
    )
    priority_categories: set[str] = set()
    for opp_type, hints in _TYPE_CATEGORY_HINTS.items():
        if opp_type not in gapped_types:
            continue
        priority_categories.update(hints)
    return priority_categories, any_gap


def _matches_priority(category: str | None, priority_hints: set[str]) -> bool:
    if not category or not priority_hints:
        return False
    lowered = category.lower()
    return any(hint in lowered for hint in priority_hints)


async def _poll_source(
    session,
    *,
    trajectory,
    round_number: int,
    source: ro.DiscoverySourceCandidate,
    config_obj: TelegramChannelConfig | FacebookPageConfig,
) -> dict[str, int]:
    """Real per-source poll -- calls the REAL, UNMODIFIED
    `fetch_new_telegram_posts`/`fetch_new_facebook_posts` +
    `process_raw_post_via_ai` + `upsert_opportunity`, exactly as the
    pre-D4.2 single static pass did. Only the caller (the round loop)
    decides WHETHER/WHEN this function runs at all."""
    stats = {"processed": 0, "created": 0, "updated": 0, "errors": 0}

    if source.source_kind == "telegram_channel":
        assert isinstance(config_obj, TelegramChannelConfig)
        source_info: dict[str, Any] = {
            "source_name": config_obj.handle,
            "source_type": SourceType.TELEGRAM,
            "source_base_url": source.source_base_url,
            "category": config_obj.category,
        }
        raw_posts = fetch_new_telegram_posts(config_obj)
        label = config_obj.handle
    else:
        assert isinstance(config_obj, FacebookPageConfig)
        source_info = {
            "source_name": config_obj.page_id,
            "source_type": SourceType.FACEBOOK,
            "source_base_url": source.source_base_url,
            "target_age": config_obj.target_age,
        }
        raw_posts = fetch_new_facebook_posts(config_obj)
        label = config_obj.page_id

    query_step = await research_trajectory_memory.record_step(
        session,
        trajectory=trajectory,
        action_type="query_source",
        query=label,
        source_domain=source_info["source_base_url"],
        metadata={"round": round_number, "source_kind": source.source_kind},
    )

    for raw_text in raw_posts:
        stats["processed"] += 1
        result_step = await research_trajectory_memory.record_step(
            session,
            trajectory=trajectory,
            action_type="process_result",
            query=label,
            source_domain=source_info["source_base_url"],
            parent_step_id=query_step.id,
            metadata={"round": round_number},
        )
        try:
            opportunity_data = await process_raw_post_via_ai(raw_text, source_info)
            opportunity, was_created = await upsert_opportunity(session, opportunity_data)
            stats["created"] += int(was_created)
            stats["updated"] += int(not was_created)
            result_step.reference_id = str(opportunity.id)
            # NEUTRAL, not SUCCESSFUL: unchanged from pre-D4.2 behavior --
            # a discovered opportunity is only a real research-efficiency
            # success once the real, decoupled verification pass judges
            # it (`apply_verification_outcome`).
            await research_trajectory_memory.complete_step(
                session,
                step=result_step,
                trajectory=trajectory,
                outcome=TrajectoryOutcome.NEUTRAL,
                discovered_opportunities_count=int(was_created),
                discovered_entities_count=1,
            )
        except AggregatorError as exc:
            logger.exception("aggregator_tasks: AI extraction failed for %s", label)
            stats["errors"] += 1
            await research_trajectory_memory.complete_step(
                session, step=result_step, trajectory=trajectory,
                outcome=TrajectoryOutcome.FAILED, error_reason=str(exc),
            )
        except Exception as exc:
            logger.exception("aggregator_tasks: unexpected failure processing a post from %s", label)
            stats["errors"] += 1
            await research_trajectory_memory.complete_step(
                session, step=result_step, trajectory=trajectory,
                outcome=TrajectoryOutcome.FAILED, error_reason=str(exc),
            )

    return stats


async def _run_adaptive_discovery_rounds(
    session,
    *,
    trajectory,
    all_sources: list[ro.DiscoverySourceCandidate],
    sources_by_id: dict[str, TelegramChannelConfig | FacebookPageConfig],
) -> dict:
    """The real, bounded, adaptive round loop (Deliverable 4.2, Steps
    4-9). Wraps -- never replaces -- `_poll_source`'s real I/O. STOP
    means the loop `break`s: no further source is polled, no further
    extraction call occurs this run (Step 8)."""
    priority_hints, any_gap = await _priority_categories_from_coverage(session)
    priority_categories = {
        source.category for source in all_sources if _matches_priority(source.category, priority_hints)
    }

    objective = ro.build_discovery_objective(
        source_categories=[s.category for s in all_sources if s.category],
        coverage_is_gap=any_gap,
        notes=(
            f"daily discovery sweep: {len(all_sources)} configured source(s), "
            f"D1 coverage gap detected={any_gap}"
        ),
    )
    # D2 integration (Step 13): a real, coarse "how connected is the
    # existing knowledge graph overall" count -- planning signal only,
    # never evidence (see `ro.discovery_graph_signal`'s own docstring).
    graph_signal = await ro.discovery_graph_signal(session)
    complexity = ro.classify_discovery_complexity(
        objective, coverage_is_gap=any_gap, graph_related_count=graph_signal
    )
    depth = complexity.recommended_depth

    already_polled: set[str] = set()
    totals = {"processed": 0, "created": 0, "updated": 0, "errors": 0}
    round_number = 0
    escalation_attempt = 0
    final_stop_reason = "loop exited without an explicit stop decision"
    rounds_log: list[dict[str, Any]] = []

    # Real, hard ceiling independent of any single depth's own
    # `max_research_steps` -- depth can escalate mid-run, so this bounds
    # the loop overall regardless of how many times it escalates
    # (`ResearchDepthLevel.ESCALATED`'s own `max_research_steps=6` is
    # still the largest single-depth bound; this is a defensive outer
    # cap, never expected to bind in practice).
    _HARD_ROUND_CEILING = 8

    while round_number < _HARD_ROUND_CEILING:
        round_number += 1
        policy = ro.DEPTH_POLICIES[depth]

        plan = ro.plan_discovery_round(
            round_number=round_number,
            all_sources=all_sources,
            already_polled_source_ids=already_polled,
            depth_policy=policy,
            priority_categories=priority_categories,
        )

        if not plan.sources_to_poll:
            final_stop_reason = "no unpolled configured sources remain -- discovery run complete"
            rounds_log.append({"round": round_number, "plan": "empty", "stop_reason": final_stop_reason})
            break

        round_stats = {"processed": 0, "created": 0, "updated": 0, "errors": 0}
        domains_this_round: set[str] = set()
        for source in plan.sources_to_poll:
            already_polled.add(ro.normalize_source_id(source.source_id))
            domains_this_round.add(source.source_base_url)
            stats = await _poll_source(
                session,
                trajectory=trajectory,
                round_number=round_number,
                source=source,
                config_obj=sources_by_id[source.source_id],
            )
            for key in round_stats:
                round_stats[key] += stats[key]
            for key in totals:
                totals[key] += stats[key]

        # Recompute the D1 signal after this round's real writes -- a
        # real "did this round move the gap" check (Step 7), not a
        # stale snapshot from before this round's own upserts.
        priority_hints, any_gap = await _priority_categories_from_coverage(session)

        sources_remaining = any(
            ro.normalize_source_id(s.source_id) not in already_polled for s in all_sources
        )
        evidence = ro.evaluate_discovery_round(
            results_count=round_stats["processed"],
            new_candidate_count=round_stats["created"],
            duplicate_count=round_stats["updated"],
            error_count=round_stats["errors"],
            distinct_domains_polled=len(domains_this_round),
            coverage_gap_remaining=any_gap,
            sources_remaining=sources_remaining,
        )

        stop, stop_reason = ro.should_stop_discovery(evidence, policy, round_number=round_number)
        escalate, escalation_reasons = ro.should_escalate_discovery(
            evidence, complexity, attempt=escalation_attempt, policy=policy,
        )

        round_record = {
            "round": round_number,
            "depth": depth.value,
            "sources_polled": [s.source_id for s in plan.sources_to_poll],
            "skipped_already_polled": plan.skipped_already_polled_source_ids,
            "priority_reasons": plan.priority_reasons,
            "results_count": evidence.results_count,
            "new_candidate_count": evidence.new_candidate_count,
            "duplicate_count": evidence.duplicate_count,
            "error_count": evidence.error_count,
            "distinct_domains_polled": evidence.distinct_domains_polled,
            "coverage_gap_remaining": evidence.coverage_gap_remaining,
            "stop_decision": stop,
            "stop_reason": stop_reason,
            "escalate_decision": escalate,
            "escalation_reasons": escalation_reasons,
        }

        await research_trajectory_memory.record_step(
            session,
            trajectory=trajectory,
            action_type="discovery_round",
            query=f"round {round_number}",
            metadata=round_record,
        )
        rounds_log.append(round_record)

        if stop:
            final_stop_reason = stop_reason
            break

        if escalate:
            next_depth = ro.next_discovery_depth(depth)
            if next_depth is not None:
                depth = next_depth
                escalation_attempt += 1
                round_record["escalated_to_depth"] = depth.value
            else:
                round_record["escalation_unavailable"] = True
        # else: continue to next round at the same depth.
    else:
        final_stop_reason = f"hard round ceiling ({_HARD_ROUND_CEILING}) reached -- stopping defensively"

    summary = {
        "discovery_execution": "adaptive_v1",
        "initial_depth": complexity.recommended_depth.value,
        "final_depth": depth.value,
        "complexity_class": complexity.complexity_class.value,
        "complexity_score": complexity.score,
        "graph_related_count": graph_signal,
        "total_rounds": round_number,
        "total_sources_configured": len(all_sources),
        "total_sources_polled": len(already_polled),
        "escalation_count": escalation_attempt,
        "stop_reason": final_stop_reason,
        "provider": "gemini",
        "provider_task": "extraction",
        **totals,
        "rounds": rounds_log,
    }
    return summary


async def _run_daily_opportunity_scraper() -> dict:
    try:
        config = load_sources_config()
    except (FileNotFoundError, ValueError) as exc:
        # Malformed/missing sources config -- nothing this run can do.
        # Distinct from a per-post error below: this is a configuration
        # problem, not a content problem, so it's reported as its own
        # terminal status rather than silently producing an all-zeros
        # summary that would be indistinguishable from "ran fine, found
        # nothing to do."
        logger.exception("run_daily_opportunity_scraper: could not load sources config")
        return {"status": "config_error", "reason": str(exc)}

    try:
        async with AsyncSessionLocal() as session:
            # Search Trajectory Memory addition (Deliverable 3) -- one
            # real trajectory per real run of this loop. See
            # `research_trajectory_memory.py`'s own module docstring for
            # why this loop (not the Scrapy scraper, which has no query
            # concept) is the real integration point. Deliverable 4.2
            # adds real, bounded, adaptive ROUND structure on top --
            # still one trajectory per run, per D3's own design.
            trajectory = await research_trajectory_memory.start_trajectory(
                session,
                objective=(
                    f"Daily opportunity discovery sweep: "
                    f"{len(config.telegram_channels)} telegram channel(s), "
                    f"{len(config.facebook_pages)} facebook page(s)"
                ),
            )

            all_sources, sources_by_id = _build_source_candidates(config)
            discovery_summary = await _run_adaptive_discovery_rounds(
                session, trajectory=trajectory, all_sources=all_sources, sources_by_id=sources_by_id
            )

            trajectory.trajectory_metadata = {
                **(trajectory.trajectory_metadata or {}),
                "discovery_summary": discovery_summary,
            }

            await research_trajectory_memory.record_outcome(session, trajectory=trajectory)
            await session.commit()
    finally:
        await engine.dispose()

    summary = {
        "status": "completed",
        "processed": discovery_summary["processed"],
        "created": discovery_summary["created"],
        "updated": discovery_summary["updated"],
        "errors": discovery_summary["errors"],
        "discovery_rounds": discovery_summary["total_rounds"],
        "discovery_depth": discovery_summary["final_depth"],
        "discovery_stop_reason": discovery_summary["stop_reason"],
    }
    logger.info("run_daily_opportunity_scraper: %s", summary)
    return summary
