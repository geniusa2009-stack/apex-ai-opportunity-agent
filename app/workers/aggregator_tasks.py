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
from app.models.enums import OpportunityType, SourceType, TrajectoryOutcome
from app.services import research_orchestrator as ro
from app.services import research_trajectory_memory
from app.services.aggregator_service import (
    AggregatorError,
    FacebookPageConfig,
    TelegramChannelConfig,
    SourcesConfig,
    load_sources_config,
    process_raw_post_via_ai,
    upsert_opportunity,
)
from app.services.source_adapters import UnsupportedSourceKindError, get_adapter
from app.services.taxonomy import normalize_category

logger = logging.getLogger(__name__)

# D1/D4 signal -> source-category prioritization (Step 5/12), Deliverable
# 5 Feature 4: the fragile, hardcoded `_TYPE_CATEGORY_HINTS` keyword dict
# this comment used to describe is REMOVED. Source categories are
# human-curated, informal, free-text/multilingual labels
# ("Internships_Training", "Self_Development", "منح") -- this pipeline
# now maps them onto the real `OpportunityType` taxonomy via the single,
# centralized `app.services.taxonomy.normalize_category` (also reusable
# by D1/D2/any future recommendation code), never a per-file keyword
# dict. Still used ONLY to decide which already-configured sources to
# poll FIRST -- never to decide what an opportunity IS (that stays
# Gemini's per-post `opportunity_type` classification, unchanged).


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
        # Phase 3B: a channel explicitly marked `"live": true` in
        # app/data/opportunity_sources.json is routed to the real
        # LiveTelegramSourceAdapter (source_kind="telegram_channel_live")
        # instead of the pre-existing mocked adapter. Every other field
        # here (category, source_base_url, D1/D2/D3/D4.2 planning) is
        # completely unaffected by this choice -- the planner/round loop
        # only ever sees a `source_kind` string and calls
        # `get_adapter(source_kind)`, exactly as before.
        candidates.append(
            ro.DiscoverySourceCandidate(
                source_id=channel.handle,
                source_kind="telegram_channel_live" if channel.live else "telegram_channel",
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


async def _gapped_opportunity_types_from_coverage(db) -> tuple[set[str], bool]:
    """D1 integration (Step 12): reuses the REAL
    `compute_opportunity_coverage` (same function `research_orchestrator`
    itself calls for verification routing) -- no duplicated query.
    Returns `(gapped_types, any_gap)`: the real, canonical `OpportunityType`
    values D1 currently flags as under-covered, and whether ANY gap
    exists at all across type/country/academic-level (used only to bias
    overall discovery depth upward -- never as evidence an opportunity
    exists)."""
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
    return gapped_types, any_gap


async def _priority_opportunity_types(db, *, gapped_types: set[str]) -> set[str]:
    """Deliverable 5, Feature 2: extends D1's `gapped_types` with a real
    D2 graph-navigation signal -- an `OpportunityType` that is genuinely
    UNDER-CONNECTED in the real knowledge graph (few real neighbor edges
    relative to the most-connected real neighbor type among currently
    relevant opportunities) is a second, independent, real reason to
    prioritize it this round, even when D1's own count-based gap check
    doesn't (also) flag it. Purely additive -- never removes a D1 gap,
    never invents a relationship the schema doesn't have (see
    `ro.discovery_graph_navigation`'s own docstring)."""
    try:
        navigation = await ro.discovery_graph_navigation(db)
    except Exception:
        logger.exception("aggregator_tasks: D2 graph navigation signal computation failed -- treating as no signal")
        return set(gapped_types)

    neighbor_type_counts: dict[str, int] = navigation.get("neighbor_opportunity_types") or {}
    if not neighbor_type_counts:
        return set(gapped_types)

    max_neighbor = max(neighbor_type_counts.values())
    under_connected = {
        opp_type.value
        for opp_type in OpportunityType
        if neighbor_type_counts.get(opp_type.value, 0) <= max_neighbor * 0.1
    }
    return set(gapped_types) | under_connected


def _matches_priority(category: str | None, gapped_types: set[str]) -> bool:
    """Deliverable 5, Feature 4: replaces the old substring-keyword
    `_TYPE_CATEGORY_HINTS` matcher with the single, centralized
    `normalize_category` -- a source's free-text/multilingual `category`
    label is prioritized when it canonically maps onto an `OpportunityType`
    D1/D2 currently flag, never via ad-hoc substring matching."""
    if not category or not gapped_types:
        return False
    canonical = normalize_category(category)
    return canonical is not None and canonical.value in gapped_types


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

    # Deliverable 5, Feature 1: fetch through the Source Adapter Layer
    # (`app.services.source_adapters`) instead of branching on
    # `source.source_kind` inline -- same real, unmodified
    # `fetch_new_telegram_posts`/`fetch_new_facebook_posts` underneath,
    # normalized into `RawDiscoveryResult`. An unsupported/unknown
    # `source_kind` is a real, honest per-source error (counted, logged,
    # trajectory-recorded) rather than a crash of the whole round.
    # Phase 3B: "telegram_channel_live" is a REAL adapter over the same
    # TelegramChannelConfig shape as the pre-existing mocked
    # "telegram_channel" -- same source_info construction, only the
    # underlying fetch differs (see source_adapters.LiveTelegramSourceAdapter).
    if source.source_kind in ("telegram_channel", "telegram_channel_live"):
        assert isinstance(config_obj, TelegramChannelConfig)
        source_info: dict[str, Any] = {
            "source_name": config_obj.handle,
            "source_type": SourceType.TELEGRAM,
            "source_base_url": source.source_base_url,
            "category": config_obj.category,
        }
        label = config_obj.handle
    else:
        assert isinstance(config_obj, FacebookPageConfig)
        source_info = {
            "source_name": config_obj.page_id,
            "source_type": SourceType.FACEBOOK,
            "source_base_url": source.source_base_url,
            "target_age": config_obj.target_age,
        }
        label = config_obj.page_id

    try:
        adapter = get_adapter(source.source_kind)
        raw_results = adapter.fetch(config_obj)
    except UnsupportedSourceKindError as exc:
        logger.error("aggregator_tasks: %s", exc)
        stats["errors"] += 1
        await research_trajectory_memory.record_step(
            session,
            trajectory=trajectory,
            action_type="query_source",
            query=label,
            source_domain=source.source_base_url,
            metadata={"round": round_number, "source_kind": source.source_kind, "error": str(exc)},
        )
        return stats
    except Exception as exc:
        # Phase 3B, Error Isolation (Section 14): a REAL adapter can now
        # raise real, classified failures (network timeout, rate limit,
        # malformed response, an unconfigured credential -- see
        # telegram_live_client.py / facebook_graph_client.py's own
        # exception hierarchies). Exactly ONE broken source must never
        # abort the whole discovery round: this is recorded as a real,
        # honest per-source failure (never a silent zero-item success)
        # and the round loop continues to the NEXT source exactly as the
        # pre-existing UnsupportedSourceKindError path already did.
        # `str(exc)` never includes secrets -- both live-client modules'
        # own exception messages are audited to reference only public
        # identifiers (handle/page_id/status code), never a token.
        logger.warning(
            "aggregator_tasks: adapter fetch failed for %s (%s): %s",
            label, source.source_kind, exc,
        )
        stats["errors"] += 1
        await research_trajectory_memory.record_step(
            session,
            trajectory=trajectory,
            action_type="query_source",
            query=label,
            source_domain=source.source_base_url,
            metadata={
                "round": round_number,
                "source_kind": source.source_kind,
                "error": str(exc),
                "error_class": type(exc).__name__,
            },
        )
        return stats

    query_step = await research_trajectory_memory.record_step(
        session,
        trajectory=trajectory,
        action_type="query_source",
        query=label,
        source_domain=source_info["source_base_url"],
        metadata={"round": round_number, "source_kind": source.source_kind, "items_fetched": len(raw_results)},
    )

    for result in raw_results:
        stats["processed"] += 1
        result_step = await research_trajectory_memory.record_step(
            session,
            trajectory=trajectory,
            action_type="process_result",
            query=label,
            source_domain=source_info["source_base_url"],
            parent_step_id=query_step.id,
            metadata={"round": round_number, "external_item_id": result.external_item_id},
        )
        try:
            # Phase 3B: per-item provenance/temporal fields ride along
            # ONLY when the adapter that produced this result actually
            # supplied them (the two pre-existing mocked adapters never
            # do, so `source_info` here is byte-identical to before this
            # phase for every non-live source -- see
            # RawDiscoveryResult's own docstring).
            item_source_info = dict(source_info)
            if result.external_item_id is not None:
                item_source_info["external_item_id"] = result.external_item_id
            if result.item_url is not None:
                item_source_info["item_url"] = result.item_url
            if result.published_at is not None:
                item_source_info["published_at"] = result.published_at
            if result.retrieved_at is not None:
                item_source_info["retrieved_at"] = result.retrieved_at

            opportunity_data = await process_raw_post_via_ai(result.raw_text, item_source_info)
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
    gapped_types, any_gap = await _gapped_opportunity_types_from_coverage(session)
    # Deliverable 5, Feature 2: D2 real graph navigation extends D1's
    # gapped-type set with real, under-connected-in-the-graph types --
    # see `_priority_opportunity_types`'s own docstring.
    priority_types = await _priority_opportunity_types(session, gapped_types=gapped_types)
    priority_categories = {
        source.category for source in all_sources if _matches_priority(source.category, priority_types)
    }
    # Deliverable 5, Feature 3: D3 backward-looking discovery memory --
    # real per-source historical yield/duplicate-rate hints from PAST
    # runs, read back BEFORE this run's own first round so they can
    # actually change this run's very first `plan_discovery_round` call
    # (a real feedback loop, not merely logged; see
    # `research_trajectory_memory.discovery_source_yield_hints`'s own
    # docstring for why this reads `discovered_opportunities_count`
    # rather than the discovery-irrelevant `outcome` field).
    yield_hints = await research_trajectory_memory.discovery_source_yield_hints(session)
    priority_source_ids = {
        ro.normalize_source_id(label) for label in yield_hints["high_yield_source_labels"]
    }
    deprioritized_source_ids = {
        ro.normalize_source_id(label) for label in yield_hints["low_yield_source_labels"]
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
            priority_source_ids=priority_source_ids,
            deprioritized_source_ids=deprioritized_source_ids,
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
        gapped_types, any_gap = await _gapped_opportunity_types_from_coverage(session)

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
        "priority_opportunity_types": sorted(priority_types),
        "high_yield_source_labels": sorted(yield_hints["high_yield_source_labels"]),
        "low_yield_source_labels": sorted(yield_hints["low_yield_source_labels"]),
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
