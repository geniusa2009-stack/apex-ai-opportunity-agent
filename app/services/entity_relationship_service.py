"""Simplified Knowledge Graph service (Deliverable 2).

Two real ways this graph gets populated, both deriving edges from data
this codebase ALREADY has on `Opportunity` (`source_id`/`country`) --
no new relationship-extraction logic, per Step 2's own instruction:

1. `sync_relationships_for_opportunity` -- real, incremental, bounded to
   one opportunity's own real matches. Wired into
   `app/workers/tasks.py::_verify_new_opportunities` so the graph stays
   current as each real opportunity is verified, without a full-table
   rescan on every run.
2. `rebuild_all_relationships` -- a real, full, idempotent rebuild over
   every existing opportunity (clears then re-derives both relationship
   types), for backfilling rows that predate this feature. Used by this
   deliverable's own live-verify proof and available to a maintenance
   script; not wired into any hot path, since a full rebuild's cost
   scales with the whole table.

SCALE BOUNDARY, DISCLOSED: grouping by `source_id` or `country` can
produce a group whose size makes pairwise edge creation expensive (a
group of size `n` produces up to `n - 1` edges for one new row, or
`n * (n - 1)` for a full rebuild of that group). `_MAX_GROUP_SIZE_FOR_PAIRWISE`
caps this -- a group larger than the cap is skipped (logged, not
silently dropped) rather than materializing an unbounded number of
rows. 50 is a deliberately small, honest default for this "affordable
tier" deliverable's real data volumes; a production deployment with
much larger per-source/per-country volumes would need a smarter
approach (e.g. capping to the N most-recent opportunities per group)
before raising this constant.
"""
import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity_relationship import EntityRelationship
from app.models.enums import RelationshipType
from app.models.opportunity import Opportunity

logger = logging.getLogger(__name__)

_MAX_GROUP_SIZE_FOR_PAIRWISE = 50


async def _add_edge(db: AsyncSession, *, opportunity_id: uuid.UUID, related_id: uuid.UUID, relationship_type: str) -> None:
    """Insert one directed edge, silently skipping a real duplicate
    (the same edge already exists) rather than crashing a whole
    population run over one already-present row. `merge`-style
    idempotency, not an error case."""
    existing = await db.execute(
        select(EntityRelationship.id).where(
            EntityRelationship.opportunity_id == opportunity_id,
            EntityRelationship.related_opportunity_id == related_id,
            EntityRelationship.relationship_type == relationship_type,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return
    db.add(
        EntityRelationship(
            opportunity_id=opportunity_id,
            related_opportunity_id=related_id,
            relationship_type=relationship_type,
        )
    )


async def sync_relationships_for_opportunity(db: AsyncSession, opportunity: Opportunity) -> int:
    """Real, incremental population: find every OTHER real opportunity
    that shares this one's real `source_id` (-> SAME_ORGANIZATION) or
    real `country` (-> SAME_COUNTRY, only when `country` is set), and
    write a real edge in BOTH directions for each real match. Bounded to
    however many real opportunities actually share that value (capped,
    see module docstring). Returns the real number of edges written.
    """
    edges_written = 0

    same_org = await db.execute(
        select(Opportunity.id).where(
            Opportunity.source_id == opportunity.source_id,
            Opportunity.id != opportunity.id,
        )
    )
    same_org_ids = [row[0] for row in same_org.all()]
    if len(same_org_ids) > _MAX_GROUP_SIZE_FOR_PAIRWISE:
        logger.warning(
            "entity_relationship_service: source_id %s has %d real opportunities -- "
            "over the pairwise cap (%d), skipping SAME_ORGANIZATION edges for opportunity %s",
            opportunity.source_id, len(same_org_ids) + 1, _MAX_GROUP_SIZE_FOR_PAIRWISE, opportunity.id,
        )
    else:
        for other_id in same_org_ids:
            await _add_edge(
                db, opportunity_id=opportunity.id, related_id=other_id,
                relationship_type=RelationshipType.SAME_ORGANIZATION.value,
            )
            await _add_edge(
                db, opportunity_id=other_id, related_id=opportunity.id,
                relationship_type=RelationshipType.SAME_ORGANIZATION.value,
            )
            edges_written += 2

    if opportunity.country:
        same_country = await db.execute(
            select(Opportunity.id).where(
                Opportunity.country == opportunity.country,
                Opportunity.id != opportunity.id,
            )
        )
        same_country_ids = [row[0] for row in same_country.all()]
        if len(same_country_ids) > _MAX_GROUP_SIZE_FOR_PAIRWISE:
            logger.warning(
                "entity_relationship_service: country %r has %d real opportunities -- "
                "over the pairwise cap (%d), skipping SAME_COUNTRY edges for opportunity %s",
                opportunity.country, len(same_country_ids) + 1, _MAX_GROUP_SIZE_FOR_PAIRWISE, opportunity.id,
            )
        else:
            for other_id in same_country_ids:
                await _add_edge(
                    db, opportunity_id=opportunity.id, related_id=other_id,
                    relationship_type=RelationshipType.SAME_COUNTRY.value,
                )
                await _add_edge(
                    db, opportunity_id=other_id, related_id=opportunity.id,
                    relationship_type=RelationshipType.SAME_COUNTRY.value,
                )
                edges_written += 2

    return edges_written


async def rebuild_all_relationships(db: AsyncSession) -> dict:
    """Real, full, idempotent rebuild: clears every existing real edge,
    then re-derives both relationship types from the real `Opportunity`
    table's current `source_id`/`country` values. Real counts returned,
    including how many real groups were skipped for exceeding the
    pairwise cap (see module docstring)."""
    await db.execute(delete(EntityRelationship))

    result = await db.execute(select(Opportunity.id, Opportunity.source_id, Opportunity.country))
    rows = result.all()

    by_source: dict[uuid.UUID, list[uuid.UUID]] = {}
    by_country: dict[str, list[uuid.UUID]] = {}
    for opp_id, source_id, country in rows:
        by_source.setdefault(source_id, []).append(opp_id)
        if country:
            by_country.setdefault(country, []).append(opp_id)

    edges_written = 0
    groups_skipped = 0

    for grouping, relationship_type in (
        (by_source, RelationshipType.SAME_ORGANIZATION.value),
        (by_country, RelationshipType.SAME_COUNTRY.value),
    ):
        for _key, ids in grouping.items():
            if len(ids) < 2:
                continue
            if len(ids) > _MAX_GROUP_SIZE_FOR_PAIRWISE:
                groups_skipped += 1
                logger.warning(
                    "rebuild_all_relationships: group of %d real opportunities over the "
                    "pairwise cap (%d) for relationship_type=%s -- skipped",
                    len(ids), _MAX_GROUP_SIZE_FOR_PAIRWISE, relationship_type,
                )
                continue
            for i, opp_id in enumerate(ids):
                for other_id in ids[i + 1:]:
                    db.add(EntityRelationship(opportunity_id=opp_id, related_opportunity_id=other_id, relationship_type=relationship_type))
                    db.add(EntityRelationship(opportunity_id=other_id, related_opportunity_id=opp_id, relationship_type=relationship_type))
                    edges_written += 2

    await db.flush()
    return {
        "real_opportunities_scanned": len(rows),
        "real_edges_written": edges_written,
        "real_groups_skipped_over_cap": groups_skipped,
    }
