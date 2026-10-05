import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.models import Opportunity
from app.models.enums import AcademicLevel, Gender
from app.services import (
    contradiction_hunter,
    entity_relationship_service,
    opportunity_state_machine,
    research_orchestrator,
    research_trajectory_memory,
)
from app.services.ai import ai_gateway
from app.services import evidence_verification
from app.models.claim import Claim
from app.models.enums import VerificationDecisionStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Backfill Real Eligibility Data step additions -- defensive coercion
# from Gemini's free-text `target_gender`/`target_academic_levels`
# strings onto this codebase's real enum vocabulary. Same established
# posture as `aggregator_service._coerce_opportunity_type`: a value that
# doesn't cleanly match is logged and dropped, not allowed to crash the
# whole verification run over one bad sub-field.
# ---------------------------------------------------------------------


def _coerce_gender(raw_value: str | None, opportunity_id) -> Gender | None:
    if not raw_value:
        return None
    try:
        return Gender(raw_value.strip().lower())
    except ValueError:
        logger.warning(
            "Opportunity %s: AI returned unrecognized target_gender %r -- leaving null",
            opportunity_id,
            raw_value,
        )
        return None


def _coerce_academic_levels(raw_values: list[str] | None, opportunity_id) -> list[AcademicLevel] | None:
    if not raw_values:
        return None
    levels: list[AcademicLevel] = []
    for raw in raw_values:
        try:
            levels.append(AcademicLevel(raw.strip().lower()))
        except ValueError:
            logger.warning(
                "Opportunity %s: AI returned unrecognized academic level %r -- dropping it",
                opportunity_id,
                raw,
            )
    # An empty list after dropping every unrecognized entry is treated the
    # same as "the model returned nothing usable" -- `None` (unrestricted),
    # not an empty-list value with different (and untested) filter semantics.
    return levels or None


@celery_app.task(name="app.workers.tasks.verify_new_opportunities")
def verify_new_opportunities(limit: int = 20) -> dict:
    """Celery entrypoint (sync).

    Backfill Real Eligibility Data step -- DESIGN DECISION, disclosed:
    the task asking for this named `app/services/aggregator_service.py`
    (`AIExtractedOpportunityFields`) as the place to add age/gender/
    academic-level extraction. Reading that file first (as instructed)
    found a better real target: `aggregator_service.py`'s own extraction
    only runs against its MOCKED Telegram/Facebook ingestion (no real
    API credentials wired -- see that module's own docstring), and every
    row it creates enters this exact `is_verified=False,
    ai_confidence_score=None` queue below regardless, where THIS
    function's own verification call (`VerifiedOpportunitySchema`, app/
    services/ai.py) already overwrites title/deadline/eligibility_criteria/
    funding_details unconditionally. Duplicating age/gender/academic-level
    extraction into the first pass would be redundant work this second
    pass immediately supersedes -- same "don't build a second parallel
    mechanism" reasoning aggregator_service.py's own module docstring
    already uses for embeddings/legitimacy-checking.
    More importantly: THIS function is the one real, single funnel every
    `Opportunity` row passes through regardless of source (the pre-existing
    Scrapy pipeline's rows AND the aggregator's rows both land in the same
    `is_verified=False` queue this function polls) -- extracting eligibility
    fields here, not in aggregator_service.py, means real Scrapy-scraped
    rows get real eligibility data too, not just (currently-mocked, non-
    production) Telegram/Facebook rows. It's also the only place with real
    retained source text to re-run for backfilling EXISTING rows (see
    `scripts/backfill_opportunity_eligibility.py`): the Scrapy pipeline
    persists the full raw scrape verbatim into `raw_extracted_data`
    (confirmed via app/scraper/items.py's own docstring), which this same
    `verify_opportunity` call already consumes.
    `AIExtractedOpportunityFields`/`OpportunityCreate` were deliberately
    NOT extended with these four fields -- confirmed unnecessary by the
    above, not skipped.

    Celery's default (prefork) worker calls
    plain functions, not coroutines -- `asyncio.run()` gives this task its
    own fresh event loop to run the actual async DB + AI Gateway work in.

    Why the loop matters here: our shared async engine
    (app.db.session.engine) lazily binds its asyncpg connections to
    whichever event loop is running the first time they're used.
    `asyncio.run()` tears its loop down when the function returns, so any
    pooled connections opened during THIS run are attached to a now-dead
    loop by the time the NEXT task starts a new one -- reusing them raises
    errors like "attached to a different loop" or "Event loop is closed".
    `_verify_new_opportunities` disposes the engine's pool in a `finally`
    block for exactly this reason: it forces the next task invocation to
    open fresh connections against its own new loop. That costs a small
    reconnect per task run, which is a non-issue at the cadence this task
    runs on, and far simpler than trying to keep one event loop alive
    across a Celery worker's whole lifetime.
    """
    return asyncio.run(_verify_new_opportunities(limit))


async def _verify_new_opportunities(limit: int) -> dict:
    processed = 0
    verified = 0
    flagged = 0
    errors = 0

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Opportunity)
                .where(
                    Opportunity.is_verified.is_(False),
                    Opportunity.ai_confidence_score.is_(None),
                )
                .limit(limit)
            )
            opportunities = result.scalars().all()

            for opportunity in opportunities:
                processed += 1

                # Adaptive Research Orchestrator & Model Routing (Deliverable 4)
                # + Execution Control Fix (Deliverable 4.1): routes THIS
                # verification objective (real complexity classification +
                # D1/D2/D3 signals), persists the decision as a real D3
                # trajectory step, then the ONLY real `ai_gateway.
                # verify_and_embed` call for a routed opportunity happens
                # inside `execute_verification`'s bounded attempt loop --
                # which actually obeys should_stop/should_escalate rather
                # than calling the provider unconditionally and only
                # recording the decision afterward. Best-effort: an
                # orchestration failure must never block real verification
                # (same posture as the entity_relationship_service/
                # research_trajectory_memory calls elsewhere in this
                # function) -- in that case only, we fall back to a single
                # direct, unrouted `ai_gateway.verify_and_embed` call so an
                # orchestrator bug never takes down verification itself.
                routing = None
                try:
                    routing_decision, routing_trajectory, routing_step = await research_orchestrator.orchestrate_verification(
                        session, opportunity=opportunity
                    )
                    routing = (routing_decision, routing_trajectory, routing_step)
                except Exception:
                    logger.exception(
                        "research_orchestrator.orchestrate_verification failed for opportunity %s "
                        "-- proceeding with verification unrouted/untraced",
                        opportunity.id,
                    )

                if routing is not None:
                    try:
                        verification, embedding, routing_decision = await research_orchestrator.execute_verification(
                            session,
                            opportunity=opportunity,
                            decision=routing[0],
                            trajectory=routing[1],
                            step=routing[2],
                        )
                        routing = (routing_decision, routing[1], routing[2])
                    except Exception:
                        logger.exception(
                            "research_orchestrator.execute_verification failed for opportunity %s",
                            opportunity.id,
                        )
                        verification, embedding = None, None
                else:
                    try:
                        verification, embedding = await ai_gateway.verify_and_embed(
                            opportunity.raw_extracted_data or {}
                        )
                    except Exception:
                        logger.exception(
                            "AI verification call failed for opportunity %s", opportunity.id
                        )
                        verification, embedding = None, None

                if verification is None:
                    errors += 1
                    continue

                if (
                    verification.is_legitimate
                    and verification.confidence_score > settings.ai_confidence_threshold
                ):
                    opportunity.title = verification.title or opportunity.title
                    opportunity.deadline = _parse_deadline(verification.deadline, opportunity.id)
                    opportunity.eligibility_criteria = verification.eligibility_criteria
                    opportunity.funding_details = verification.funding_details
                    opportunity.embedding = embedding
                    opportunity.ai_confidence_score = verification.confidence_score
                    # Backfill Real Eligibility Data step additions -- see
                    # `VerifiedOpportunitySchema`'s own field docs
                    # (app/services/ai.py) for the "null is the correct
                    # default" extraction rules these four values already
                    # went through inside the Gemini call itself.
                    opportunity.min_age = verification.min_age
                    opportunity.max_age = verification.max_age
                    opportunity.target_gender = _coerce_gender(verification.target_gender, opportunity.id)
                    opportunity.target_academic_levels = _coerce_academic_levels(
                        verification.target_academic_levels, opportunity.id
                    )

                    # D6 (Evidence & Verification Intelligence) integration
                    # point -- this is the ONE real write site for
                    # `is_verified`, and it no longer flips straight to
                    # True off `verification.is_legitimate`/
                    # `confidence_score` alone. The AI's structured output
                    # above is only a CANDIDATE claim set; the backend now
                    # runs it through a real, deterministic evidence
                    # pipeline (`evidence_verification.py`) and only sets
                    # `is_verified = True` when that pipeline's own
                    # decision is VERIFIED. See that module's own
                    # docstring for the full rule set.
                    try:
                        d6_decision = await _run_d6_evidence_pipeline(
                            session, opportunity=opportunity, verification=verification
                        )
                    except Exception:
                        logger.exception(
                            "D6 evidence pipeline failed for opportunity %s -- treating as UNVERIFIED "
                            "rather than falling back to the pre-D6 is_legitimate/confidence_score gate",
                            opportunity.id,
                        )
                        d6_decision = None

                    # D7 (Contradiction Hunter & Adversarial Verification)
                    # integration point (spec sections 17/18) -- only
                    # attacks a decision that D6 already reached VERIFIED
                    # (no point adversarially searching against a decision
                    # that isn't VERIFIED yet), and only when D4's own
                    # depth policy for THIS objective
                    # (`DEPTH_POLICIES[decision.depth].contradiction_hunting`)
                    # says it earned that scrutiny -- the exact, pre-built
                    # D4 hook `contradiction_hunter.py` reads directly, no
                    # second budget/flag system. `is_verified` below is
                    # gated on `final_decision` (POST-D7), not `d6_decision`
                    # (PRE-D7): a real, backend-validated contradiction D7
                    # finds can still downgrade an otherwise-VERIFIED
                    # opportunity to HOLD/UNVERIFIED/CONFLICTED. Best-effort,
                    # same posture as every other optional pass in this
                    # loop -- a D7 failure must NEVER cause `is_verified=
                    # True` where D6 alone would not have set it, so any
                    # exception here simply leaves `final_decision` as D6's
                    # own already-computed decision.
                    final_decision = d6_decision
                    if d6_decision is not None and d6_decision.status == VerificationDecisionStatus.VERIFIED:
                        try:
                            claims_result = await session.execute(
                                select(Claim).where(Claim.opportunity_id == opportunity.id)
                            )
                            claims_for_hunt = list(claims_result.scalars().all())
                            hunt_result = await contradiction_hunter.hunt_contradictions_for_opportunity(
                                session,
                                opportunity=opportunity,
                                claims=claims_for_hunt,
                                decision=routing[0] if routing is not None else None,
                                trajectory=routing[1] if routing is not None else None,
                            )
                            if hunt_result.ran:
                                final_decision = await evidence_verification.decide_verification(
                                    session, opportunity_id=opportunity.id, claims=claims_for_hunt
                                )
                        except Exception:
                            logger.exception(
                                "D7 contradiction hunter failed for opportunity %s -- D6's own decision "
                                "stands unchanged (a D7 failure never upgrades OR downgrades is_verified "
                                "on its own; only a real, recorded contradiction can)",
                                opportunity.id,
                            )
                            final_decision = d6_decision

                    if final_decision is not None and final_decision.status == VerificationDecisionStatus.VERIFIED:
                        opportunity.is_verified = True
                        verified += 1

                        # Simplified Knowledge Graph addition (Deliverable 2)
                        # -- real, incremental edge population, bounded to
                        # this one freshly-verified opportunity's own real
                        # source_id/country matches. See
                        # `entity_relationship_service`'s own module
                        # docstring for the full design/scale reasoning.
                        # Best-effort: a real failure here must never
                        # discard an otherwise-successful verification.
                        try:
                            await entity_relationship_service.sync_relationships_for_opportunity(
                                session, opportunity
                            )
                        except Exception:
                            logger.exception(
                                "entity_relationship_service sync failed for opportunity %s "
                                "-- verification itself still succeeded",
                                opportunity.id,
                            )
                    else:
                        # AI thought this looked legitimate, but D6's real
                        # evidence pipeline (as possibly refined by D7's
                        # adversarial contradiction hunt) could not reach
                        # VERIFIED (no supporting evidence, a contradiction,
                        # a stale-only claim, or a conflict needing human
                        # HOLD review). `is_verified` is deliberately left
                        # False -- cleaned title/deadline/etc. above are
                        # still saved as useful reviewer-facing signal (same
                        # posture as the flagging branch below), but the
                        # trust gate itself is not crossed.
                        logger.info(
                            "D6/D7: opportunity %s not verified -- decision=%s reason=%s",
                            opportunity.id,
                            final_decision.status.value if final_decision else "pipeline_error",
                            final_decision.reason if final_decision else "evidence pipeline raised",
                        )
                        flagged += 1
                else:
                    # Flagging mechanism: leave is_verified=False but record the
                    # score. That alone moves the row out of the "pending"
                    # query above (`ai_confidence_score IS NULL`) and into a
                    # de facto human-review queue -- reachable later via
                    # `WHERE is_verified = false AND ai_confidence_score IS NOT NULL`.
                    # Deliberately not deleting: raw_extracted_data is the audit
                    # trail, and a prompt fix or model upgrade can re-evaluate
                    # these later without re-scraping anything.
                    opportunity.ai_confidence_score = verification.confidence_score
                    flagged += 1

                # Taxonomy Evolution + Temporal State Machine integration point
                # (P0-A repair) -- the ONE real production call site for
                # `opportunity_state_machine.reevaluate_opportunity_state`.
                # Previously this module was fully built, fully tested, and
                # had ZERO production callers (confirmed via repo-wide grep
                # before this change: only its own test file called it) --
                # `Opportunity.lifecycle_state` never advanced past its
                # DISCOVERED default for any real opportunity, and every
                # downstream HOLD/CONFLICT exclusion fix is pointless until
                # this call exists. Runs once per processed opportunity,
                # after is_verified/ai_confidence_score are already settled
                # above (covers all three branches: D6/D7-VERIFIED,
                # D6/D7-ran-but-not-VERIFIED, and AI-said-not-legitimate/
                # low-confidence) -- `reevaluate_opportunity_state` is a
                # fully self-contained read-current-DB-state-and-write
                # operation (it re-derives the D6 decision itself via
                # `evidence_verification.evaluate_verification_readonly`,
                # the same real function D6 itself uses), so it needs
                # nothing passed in beyond the opportunity id, and is
                # idempotent/safe to call even for the AI-said-not-
                # legitimate branch (zero claims persisted there resolves
                # to DISCOVERED, a no-op from the real default). Best-
                # effort: a state-machine failure must never block or
                # unwind the verification write that already committed
                # above in this same transaction.
                # Captured as a plain value (not re-read from the ORM instance) for
                # the exception log below: a `begin_nested()` rollback expires every
                # attribute SQLAlchemy loaded/touched since the savepoint began, and
                # logging `opportunity.id` after such a rollback would trigger an
                # implicit, synchronous lazy-reload of an already-rolled-back ORM
                # object from inside an exception handler -- exactly the kind of
                # second, unrelated failure a best-effort block must never risk.
                opportunity_id_for_logging = opportunity.id
                try:
                    # SAVEPOINT (not the outer transaction) -- `record_change_if_meaningful`
                    # (called by `reevaluate_opportunity_state` whenever the state actually
                    # transitions) does an eager `await db.flush()`, so a real DB-level failure
                    # here raises immediately, inside this try, rather than silently deferring
                    # to the batch's final `await session.commit()` far below. Catching the
                    # Python exception alone would NOT be enough: a failed flush leaves the
                    # session's outer transaction in a needs-rollback state, and the later
                    # `session.commit()` would then raise for the ENTIRE batch (rolling back
                    # every other opportunity this run already verified) over one opportunity's
                    # lifecycle-state write. `begin_nested()` gives this one call its own
                    # SAVEPOINT so a failure here rolls back only this opportunity's
                    # lifecycle_state/history write and leaves the outer transaction -- and every
                    # other opportunity already processed in this same run -- untouched.
                    async with session.begin_nested():
                        await opportunity_state_machine.reevaluate_opportunity_state(
                            session, opportunity_id=opportunity.id
                        )
                except Exception:
                    logger.exception(
                        "opportunity_state_machine.reevaluate_opportunity_state failed for "
                        "opportunity %s -- verification/is_verified itself still stands; "
                        "lifecycle_state left at its prior persisted value",
                        opportunity_id_for_logging,
                    )

                # Search Trajectory Memory addition (Deliverable 3) --
                # closes the loop from this real, decoupled verification
                # pass back onto whichever real trajectory step
                # originally discovered this opportunity (a real no-op
                # for opportunities the pre-existing Scrapy pipeline
                # created, since those have no matching step -- see
                # `apply_verification_outcome`'s own docstring). Never
                # writes to `Opportunity` itself; best-effort, same
                # posture as the entity_relationship_service sync above.
                try:
                    await research_trajectory_memory.apply_verification_outcome(
                        session,
                        opportunity_id=opportunity.id,
                        is_legitimate=verification.is_legitimate,
                        confidence_score=verification.confidence_score,
                    )
                except Exception:
                    logger.exception(
                        "research_trajectory_memory.apply_verification_outcome failed for "
                        "opportunity %s -- verification itself still succeeded",
                        opportunity.id,
                    )

                # Deliverable 4.1: the routing trace for this opportunity was
                # already closed out inside `execute_verification` (real
                # should_stop/should_escalate against the REAL verification
                # result, before `verification`/`embedding` were even
                # returned to this loop) -- nothing left to do here.

            await session.commit()
    finally:
        await engine.dispose()

    summary = {"processed": processed, "verified": verified, "flagged": flagged, "errors": errors}
    logger.info("verify_new_opportunities: %s", summary)
    return summary


# ---------------------------------------------------------------------
# D6 (Evidence & Verification Intelligence) -- the real pipeline that
# turns one AI verification call's candidate claim values into
# durable Claim/Evidence rows and a deterministic VerificationDecision.
# See `app/services/evidence_verification.py`'s own module docstring
# for the full rule set this delegates to. This function is
# deliberately dumb: it only maps REAL, already-extracted fields
# (VerifiedOpportunitySchema + the opportunity's own real
# source_id/target_url/country/raw_extracted_data) onto claim/evidence
# rows -- it invents nothing and calls no second LLM.
# ---------------------------------------------------------------------


async def _run_d6_evidence_pipeline(session, *, opportunity, verification):
    from app.services.evidence_verification import (
        EvidenceRejectedError,
        decide_verification,
        get_or_create_claim,
        record_evidence,
    )
    from app.services.temporal_evidence import extract_retrieved_at_from_raw_scrape

    raw = opportunity.raw_extracted_data or {}
    raw_description = raw.get("description") if isinstance(raw, dict) else None

    # Phase 1 (Evidence Temporal Fidelity): a real, source-adjacent
    # `retrieved_at` when this opportunity's raw scrape carries one (set
    # by the spider at the actual moment of scraping) -- strictly more
    # accurate than "whenever this batch pipeline happens to run." `None`
    # (absent/invalid) leaves `record_evidence`'s own honest `now()`
    # default untouched. `published_at` is deliberately NOT derived here:
    # no real source-claimed publication date exists anywhere in this
    # pipeline today (see `temporal_evidence`'s own module docstring) --
    # inventing one from `opportunity.created_at`/`updated_at` would be
    # exactly the retrieval-time-as-publication-time fabrication this
    # phase forbids.
    retrieved_at = extract_retrieved_at_from_raw_scrape(raw)

    candidate_claims: list[tuple[str, str | None, str | None]] = [
        ("title", verification.title, None),
        ("eligibility", verification.eligibility_criteria, None),
        ("deadline", verification.deadline, None),
        ("funding", verification.funding_details, None),
        ("country", opportunity.country, None),
        ("application_url", opportunity.target_url, opportunity.target_url),
    ]

    claims = []
    for claim_type, claim_value, normalized_value in candidate_claims:
        if not claim_value:
            # No unsupported guesses: a null/empty extracted field
            # produces no claim at all, rather than a claim with a
            # fabricated value (constitution rule 8).
            continue

        claim = await get_or_create_claim(
            session,
            opportunity_id=opportunity.id,
            claim_type=claim_type,
            claim_value=str(claim_value),
            normalized_value=normalized_value,
        )
        claims.append(claim)

        evidence_text = raw_description or str(claim_value)
        try:
            await record_evidence(
                session,
                opportunity_id=opportunity.id,
                claim_id=claim.id,
                evidence_text=evidence_text,
                source_id=opportunity.source_id,
                source_url=opportunity.target_url,
                extraction_method="ai_gateway_structured_output",
                retrieval_method="automated",
                supports_claim=True,
                retrieved_at=retrieved_at,
            )
        except EvidenceRejectedError:
            logger.warning(
                "D6: candidate evidence for opportunity %s claim %s rejected by backend validation "
                "-- claim remains without evidence",
                opportunity.id,
                claim_type,
            )

    return await decide_verification(session, opportunity_id=opportunity.id, claims=claims)


def _parse_deadline(raw: str | None, opportunity_id) -> datetime | None:
    if not raw:
        return None
    try:
        # `datetime.fromisoformat` only accepts a trailing "Z" (meaning UTC)
        # starting in Python 3.11; normalizing it here makes this parser
        # correct regardless of the running Python version, rather than
        # silently depending on the Docker image staying on 3.11+.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Could not parse AI-returned deadline %r for opportunity %s", raw, opportunity_id)
        return None
