"""One-off backfill script -- Backfill Real Eligibility Data step.

Re-runs the real Gemini verification call (`ai_gateway.verify_opportunity`,
app/services/ai.py) against EXISTING `Opportunity` rows' already-retained
`raw_extracted_data`, and writes ONLY the four new eligibility columns
(`min_age`, `max_age`, `target_gender`, `target_academic_levels`) -- every
other column on the row (title, deadline, eligibility_criteria,
funding_details, embedding, ai_confidence_score, is_verified) is left
completely untouched, on purpose: this script's only job is to fill in a
gap on rows that already went through verification before these four
fields existed, not to re-verify or re-embed anything.

NOT a permanent endpoint or Celery task -- run manually, once, per this
step's own explicit instruction. Real proof of a dry run is required
before any real `--apply` run against production data (see the final
report for this step).

DISCLOSED, IMPORTANT ASYMMETRY BETWEEN THE TWO SOURCE PIPELINES this
script backfills, found while investigating feasibility (Step 3 of this
task):

- Rows from the pre-existing Scrapy pipeline (`app/scraper/pipelines.py`)
  have `raw_extracted_data` set to the FULL raw scraped dict (title,
  description, eligibility_criteria, funding_details, location, country,
  scraped_at -- confirmed via `app/scraper/items.py`'s own docstring: "the
  original scrape is never lost"). This is genuinely the real source text
  the Gemini call can re-extract eligibility restrictions from.
- Rows from `aggregator_service.py`'s Telegram/Facebook pipeline have
  `raw_extracted_data` set to `OpportunityCreate.model_dump(...)` --
  i.e. the ALREADY-CLEANED, ALREADY-EXTRACTED output (title/description/
  target_url/type/deadline/country/is_local_egypt/target_age_group), NOT
  the original raw post text. That original text (the `raw_text` argument
  to `process_raw_post_via_ai`) is never persisted anywhere. Backfilling
  these rows re-analyzes an already-summarized description rather than
  the real original wording -- strictly weaker material, more likely to
  under-extract a restriction that was present in the original post but
  dropped by the first (unrelated) summarization pass. This script still
  attempts it (a summarized description is better than nothing, and this
  pipeline's own ingestion is currently MOCKED/non-production anyway --
  see aggregator_service.py's own module docstring), but this asymmetry
  is reported in this script's own summary output, not hidden.
- Rows with `raw_extracted_data IS NULL` (any row inserted before this
  column existed, or via a path that never populated it) cannot be
  backfilled at all -- no source text to re-extract from. This script
  counts and reports them separately rather than silently skipping them.

Usage:
    python -m scripts.backfill_opportunity_eligibility            # dry run (default)
    python -m scripts.backfill_opportunity_eligibility --apply    # writes for real
    python -m scripts.backfill_opportunity_eligibility --limit 50 # cap batch size
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.session import AsyncSessionLocal, engine  # noqa: E402
from app.models.opportunity import Opportunity  # noqa: E402
from app.services.ai import ai_gateway  # noqa: E402
from app.workers.tasks import _coerce_academic_levels, _coerce_gender  # noqa: E402

logger = logging.getLogger(__name__)


async def _run(*, apply: bool, limit: int | None) -> dict:
    already_backfilled = 0
    no_raw_data = 0
    processed = 0
    updated = 0
    errors = 0
    samples: list[dict] = []

    async with AsyncSessionLocal() as db:
        # Only rows not already touched by this backfill (or by a normal
        # verification run that already populated these columns for real)
        # -- re-running against already-populated rows would silently
        # overwrite a real, already-correct value with a fresh guess for
        # no reason. `raw_extracted_data.isnot(None)` is the real
        # feasibility gate described in the module docstring above.
        query = select(Opportunity).where(
            Opportunity.raw_extracted_data.isnot(None),
            Opportunity.min_age.is_(None),
            Opportunity.max_age.is_(None),
            Opportunity.target_gender.is_(None),
            Opportunity.target_academic_levels.is_(None),
        )
        if limit is not None:
            query = query.limit(limit)
        candidates = (await db.execute(query)).scalars().all()

        no_raw_data_result = await db.execute(
            select(Opportunity.id).where(Opportunity.raw_extracted_data.is_(None))
        )
        no_raw_data = len(no_raw_data_result.all())

        already_backfilled_result = await db.execute(
            select(Opportunity.id).where(
                Opportunity.raw_extracted_data.isnot(None),
                (
                    Opportunity.min_age.isnot(None)
                    | Opportunity.max_age.isnot(None)
                    | Opportunity.target_gender.isnot(None)
                    | Opportunity.target_academic_levels.isnot(None)
                ),
            )
        )
        already_backfilled = len(already_backfilled_result.all())

        for opportunity in candidates:
            processed += 1
            try:
                verification = await ai_gateway.verify_opportunity(opportunity.raw_extracted_data or {})
            except Exception:
                logger.exception("Backfill: verification call failed for opportunity %s", opportunity.id)
                errors += 1
                continue

            new_gender = _coerce_gender(verification.target_gender, opportunity.id)
            new_levels = _coerce_academic_levels(verification.target_academic_levels, opportunity.id)
            new_min_age = verification.min_age
            new_max_age = verification.max_age

            would_change = any(
                value is not None for value in (new_min_age, new_max_age, new_gender, new_levels)
            )
            if would_change:
                samples.append(
                    {
                        "id": str(opportunity.id),
                        "title": opportunity.title,
                        "min_age": new_min_age,
                        "max_age": new_max_age,
                        "target_gender": new_gender.value if new_gender else None,
                        "target_academic_levels": [lvl.value for lvl in new_levels] if new_levels else None,
                    }
                )
                if apply:
                    opportunity.min_age = new_min_age
                    opportunity.max_age = new_max_age
                    opportunity.target_gender = new_gender
                    opportunity.target_academic_levels = new_levels
                updated += 1

        if apply:
            await db.commit()
        else:
            await db.rollback()

    return {
        "mode": "APPLY (real writes committed)" if apply else "DRY RUN (nothing written)",
        "already_backfilled_skipped": already_backfilled,
        "no_raw_extracted_data_unbackfillable": no_raw_data,
        "candidates_processed": processed,
        "rows_with_a_new_restriction_found": updated,
        "errors": errors,
        "sample_changes": samples[:10],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes. Without this flag, runs a dry run and commits nothing.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap how many candidate rows to process this run."
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    try:
        summary = asyncio.run(_run(apply=args.apply, limit=args.limit))
    finally:
        asyncio.run(engine.dispose())

    print("\n=== Backfill Real Eligibility Data -- summary ===")
    for key, value in summary.items():
        if key == "sample_changes":
            print(f"{key}:")
            for sample in value:
                print(f"  {sample}")
        else:
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
