"""Live Source Smoke Test (Section 22) -- a SEPARATE command from the
pytest suite, per this task's own explicit instruction not to mix unit
tests / integration tests / live smoke tests.

Usage (from the repository root, with the venv active):

    python -m app.services.source_enablement_smoke_test --source-id src-ministry-of-communications-and-information-technology-mcit
    python -m app.services.source_enablement_smoke_test --tier TIER_1_PRIMARY --limit 5
    python -m app.services.source_enablement_smoke_test --all --limit 10

Guarantees, enforced by this module's own code (not just documentation):
- NEVER modifies `Opportunity.is_verified` or any verification state --
  this module imports nothing from `app.services.evidence_verification`,
  `app.services.contradiction_hunter`, or `app.workers.tasks`.
- NEVER writes to the database at all -- no `AsyncSession` is opened
  anywhere in this file.
- NEVER auto-publishes an opportunity -- fetched items are written only
  to a machine-readable JSON report on disk, never inserted as
  `Opportunity` rows.
- Only ever marks a source `live` in its OWN report output when an
  actual fetch+parse round-trip succeeded in THIS run -- never from
  adapter-class existence, and never carried over from a previous run.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import sys

from app.services import source_registry
from app.services.source_enablement import SourceFetchResult, run_source_enablement_batch


def _select_sources(args: argparse.Namespace) -> list[dict]:
    if args.source_id:
        source = source_registry.get_source_by_id(args.source_id)
        if source is None:
            print(f"no such source_id: {args.source_id}", file=sys.stderr)
            sys.exit(2)
        return [source]
    if args.tier:
        sources = source_registry.get_sources_by_tier(args.tier)
    else:
        sources = source_registry.load_source_registry()["sources"]
    if args.limit:
        sources = sources[: args.limit]
    return sources


def _serialize_result(result: SourceFetchResult) -> dict:
    d = dataclasses.asdict(result)
    d["fetched_at"] = result.fetched_at.isoformat()
    for item in d["raw_items"]:
        item["discovered_at"] = item["discovered_at"].isoformat() if isinstance(item.get("discovered_at"), dt.datetime) else item.get("discovered_at")
        if isinstance(item.get("published_at"), dt.datetime):
            item["published_at"] = item["published_at"].isoformat()
        # Report only counts/metadata for raw content -- avoid dumping
        # full scraped page text into a committed report artifact.
        item["raw_content_length"] = len(item.pop("raw_content", "") or "")
    return d


async def _main_async(args: argparse.Namespace) -> dict:
    sources = _select_sources(args)
    results = await run_source_enablement_batch(sources, concurrency_limit=args.concurrency)
    report = {
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sources_attempted": len(results),
        "live_count": sum(1 for r in results if r.success and r.health_status == "healthy"),
        "failed_count": sum(1 for r in results if not r.success),
        "results": [_serialize_result(r) for r in results],
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="APEX Source Enablement live smoke test (NEVER mocked -- real network calls).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-id", help="Run against exactly one source_id.")
    group.add_argument("--tier", choices=["TIER_1_PRIMARY", "TIER_2_AGGREGATOR", "TIER_3_COMMUNITY"], help="Run against every source in a tier.")
    group.add_argument("--all", action="store_true", help="Run against every source in the registry.")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of sources attempted.")
    parser.add_argument("--concurrency", type=int, default=3, help="Max concurrent fetches (bounded, per Section 27).")
    parser.add_argument("--output", default=None, help="Path to write the JSON report. Defaults to stdout only.")
    args = parser.parse_args()

    report = asyncio.run(_main_async(args))
    output_json = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"wrote {args.output}: {report['sources_attempted']} attempted, {report['live_count']} live, {report['failed_count']} failed")
    else:
        print(output_json)


if __name__ == "__main__":
    main()
