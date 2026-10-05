"""Multi-layer deduplication.

Layers (spec section 14):

1. normalized URL                     (fingerprint kind ``url``)
2. normalized application URL         (``application_url``)
3. normalized title / org+title       (``title``, ``org_title``)
4. content hash                       (``content_hash``)
5. semantic similarity                (embeddings, Postgres/pgvector) -- and a
   deterministic lexical title scan as a zero-cost fallback

Layers 1-4 are exact lookups on `opportunity_fingerprints`. Layer 5 only
PROPOSES candidates. Every final decision is made by `compare()`, a pure,
deterministic function over organization, title, deadline, application /
official URL, opportunity type and edition year. **Similarity alone can
never produce DUPLICATE** -- at most POSSIBLE_DUPLICATE, which a human
resolves.

Generic URLs (a site's home page, e.g. ``https://daad.de``) are not
identity: many opportunities share them. Only *specific* URLs (with a
path or query) count as a decisive URL match.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.dates import find_dates, parse_date
from app.agent.normalization import (
    normalize_organization,
    normalize_title,
    normalize_url,
    sha256_hex,
    title_similarity,
)
from app.models.agent import AgentOpportunityFacts, OpportunityFingerprint
from app.models.agent_enums import DedupOutcome, FingerprintKind
from app.models.opportunity import Opportunity

# Thresholds. Title similarity is token Jaccard on normalized titles.
STRONG_TITLE_SIMILARITY = 0.85
CANDIDATE_TITLE_SIMILARITY = 0.5
LEXICAL_SCAN_LIMIT = 500

_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")

_OUTCOME_RANK = {
    DedupOutcome.NEW: 0,
    DedupOutcome.POSSIBLE_DUPLICATE: 1,
    DedupOutcome.UPDATE_EXISTING: 2,
    DedupOutcome.DUPLICATE: 3,
}


@dataclasses.dataclass
class DedupFacts:
    """The comparable facts of one opportunity (new or existing)."""

    title: str | None = None
    organization: str | None = None
    opportunity_type: str | None = None
    deadline: dt.date | None = None
    application_url: str | None = None
    official_url: str | None = None
    target_url: str | None = None
    funding: str | None = None
    eligibility: str | None = None
    content_hash: str | None = None

    @property
    def norm_title(self) -> str:
        return normalize_title(self.title)

    @property
    def norm_org(self) -> str:
        return normalize_organization(self.organization)

    def specific_urls(self) -> set[str]:
        urls = set()
        for url in (self.application_url, self.official_url, self.target_url):
            key = normalize_url(url)
            if key and is_specific_url(key):
                urls.add(key)
        return urls

    @property
    def norm_application_url(self) -> str | None:
        key = normalize_url(self.application_url)
        return key if key and is_specific_url(key) else None

    @property
    def edition_years(self) -> frozenset[int]:
        return frozenset(int(y) for y in _YEAR.findall(self.title or ""))


@dataclasses.dataclass
class Comparison:
    outcome: DedupOutcome
    reasons: list[str]
    signals: dict[str, Any]


@dataclasses.dataclass
class DedupDecision:
    outcome: DedupOutcome
    opportunity_id: uuid.UUID | None
    reasons: list[str]
    candidates: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "opportunity_id": str(self.opportunity_id) if self.opportunity_id else None,
            "reasons": self.reasons,
            "candidates": self.candidates,
        }


def is_specific_url(normalized: str) -> bool:
    """A URL identifies one thing only if it has a non-trivial path or a query."""
    parts = urlsplit(normalized)
    path = (parts.path or "").strip("/")
    if parts.query:
        return True
    if not path:
        return False
    # "/en", "/ar", "/home", "/index.html" are still home pages.
    return path.lower() not in {"en", "ar", "fr", "home", "index", "index.html", "index.php", "main"}


def _norm_type(value: str | None) -> str | None:
    return value.strip().lower() if value else None


def compare(new: DedupFacts, existing: DedupFacts) -> Comparison:
    """Deterministic duplicate decision between two opportunities."""
    reasons: list[str] = []
    title_eq = bool(new.norm_title) and new.norm_title == existing.norm_title
    title_sim = title_similarity(new.title, existing.title)
    org_known = bool(new.norm_org) and bool(existing.norm_org)
    org_eq = org_known and (
        new.norm_org == existing.norm_org or new.norm_org in existing.norm_org or existing.norm_org in new.norm_org
    )
    org_conflict = org_known and not org_eq
    new_urls, existing_urls = new.specific_urls(), existing.specific_urls()
    # Application-level match: the specific application URL of one side is
    # the application URL -- or the specific target/official URL -- of the
    # other (legacy scraped rows only know `target_url`).
    app_url_eq = bool(new.norm_application_url and new.norm_application_url in existing_urls) or bool(
        existing.norm_application_url and existing.norm_application_url in new_urls
    )
    url_overlap = bool(new_urls & existing_urls)
    hash_eq = bool(new.content_hash) and new.content_hash == existing.content_hash
    deadline_known = new.deadline is not None and existing.deadline is not None
    deadline_eq = deadline_known and new.deadline == existing.deadline
    deadline_conflict = deadline_known and new.deadline != existing.deadline
    type_conflict = bool(_norm_type(new.opportunity_type)) and bool(_norm_type(existing.opportunity_type)) and (
        _norm_type(new.opportunity_type) != _norm_type(existing.opportunity_type)
        and "other" not in (_norm_type(new.opportunity_type), _norm_type(existing.opportunity_type))
    )
    year_conflict = bool(new.edition_years) and bool(existing.edition_years) and not (
        new.edition_years & existing.edition_years
    )
    fills_gaps = any(
        getattr(new, field) and not getattr(existing, field) for field in ("deadline", "funding", "eligibility")
    ) or bool(new.norm_application_url and new.norm_application_url not in existing_urls)
    signals = {
        "title_equal": title_eq,
        "title_similarity": round(title_sim, 3),
        "organization_equal": org_eq,
        "organization_conflict": org_conflict,
        "application_url_equal": app_url_eq,
        "specific_url_overlap": url_overlap,
        "content_hash_equal": hash_eq,
        "deadline_equal": deadline_eq,
        "deadline_conflict": deadline_conflict,
        "type_conflict": type_conflict,
        "edition_year_conflict": year_conflict,
        "fills_missing_fields": fills_gaps,
    }

    if hash_eq:
        return Comparison(DedupOutcome.DUPLICATE, ["identical normalized content"], signals)

    if year_conflict:
        reasons.append(f"different edition year {sorted(new.edition_years)} vs {sorted(existing.edition_years)}")
        same_program = title_similarity(_YEAR.sub(" ", new.title or ""), _YEAR.sub(" ", existing.title or ""))
        if app_url_eq or same_program >= STRONG_TITLE_SIMILARITY:
            return Comparison(DedupOutcome.POSSIBLE_DUPLICATE, reasons + ["same program, new cycle?"], signals)
        return Comparison(DedupOutcome.NEW, reasons, signals)

    hard_conflict = org_conflict or type_conflict
    strong_identity = app_url_eq or (url_overlap and (title_sim >= CANDIDATE_TITLE_SIMILARITY or org_eq)) or (
        title_eq and (org_eq or not org_known)
    )

    if strong_identity and not hard_conflict:
        if app_url_eq:
            reasons.append("same specific application URL")
        if url_overlap and not app_url_eq:
            reasons.append("same specific official/target URL")
        if title_eq:
            reasons.append("same normalized title" + (" and organization" if org_eq else ""))
        if deadline_conflict:
            reasons.append(f"deadline changed {existing.deadline} -> {new.deadline}")
            if org_eq or app_url_eq:
                return Comparison(DedupOutcome.UPDATE_EXISTING, reasons, signals)
            return Comparison(DedupOutcome.POSSIBLE_DUPLICATE, reasons, signals)
        if fills_gaps:
            return Comparison(DedupOutcome.UPDATE_EXISTING, reasons + ["new message adds missing facts"], signals)
        if not org_known and not app_url_eq and not deadline_eq:
            # Same title but nothing else to confirm it: let a human decide.
            return Comparison(DedupOutcome.POSSIBLE_DUPLICATE, reasons + ["organization unknown"], signals)
        return Comparison(DedupOutcome.DUPLICATE, reasons, signals)

    if strong_identity and hard_conflict:
        reasons.append("strong identity signal but conflicting " + ("organization" if org_conflict else "type"))
        return Comparison(DedupOutcome.POSSIBLE_DUPLICATE, reasons, signals)

    # Candidate-only territory (similarity): never DUPLICATE.
    if title_sim >= CANDIDATE_TITLE_SIMILARITY and not hard_conflict and (org_eq or deadline_eq or url_overlap):
        reasons.append(f"similar title ({title_sim:.2f}) with a matching attribute")
        return Comparison(DedupOutcome.POSSIBLE_DUPLICATE, reasons, signals)
    if title_sim >= STRONG_TITLE_SIMILARITY and not hard_conflict:
        reasons.append(f"very similar title ({title_sim:.2f}) but no confirming attribute")
        return Comparison(DedupOutcome.POSSIBLE_DUPLICATE, reasons, signals)
    return Comparison(DedupOutcome.NEW, ["no deterministic match"], signals)


# --------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------

def fingerprint_values(facts: DedupFacts) -> list[tuple[FingerprintKind, str]]:
    values: list[tuple[FingerprintKind, str]] = []
    for url in sorted(facts.specific_urls()):
        values.append((FingerprintKind.URL, url))
    if facts.norm_application_url:
        values.append((FingerprintKind.APPLICATION_URL, facts.norm_application_url))
    if facts.norm_title:
        values.append((FingerprintKind.TITLE, facts.norm_title))
        if facts.norm_org:
            values.append((FingerprintKind.ORG_TITLE, f"{facts.norm_org}|{facts.norm_title}"))
    if facts.content_hash:
        values.append((FingerprintKind.CONTENT_HASH, facts.content_hash))
    return values


async def record_fingerprints(db: AsyncSession, opportunity_id: uuid.UUID, facts: DedupFacts) -> int:
    """Idempotently store the fingerprints of an opportunity."""
    existing = {
        (row.kind, row.value_hash)
        for row in (
            await db.execute(select(OpportunityFingerprint).where(OpportunityFingerprint.opportunity_id == opportunity_id))
        ).scalars()
    }
    added = 0
    for kind, value in fingerprint_values(facts):
        key = (kind.value, sha256_hex(value))
        if key in existing:
            continue
        db.add(OpportunityFingerprint(opportunity_id=opportunity_id, kind=kind.value, value_hash=key[1], value=value))
        existing.add(key)
        added += 1
    await db.flush()
    return added


async def exact_matches(db: AsyncSession, facts: DedupFacts) -> dict[uuid.UUID, set[str]]:
    """Layers 1-4: opportunity id -> fingerprint kinds that matched exactly."""
    pairs = [(kind.value, sha256_hex(value)) for kind, value in fingerprint_values(facts)]
    if not pairs:
        return {}
    hashes = {h for _, h in pairs}
    rows = (
        await db.execute(select(OpportunityFingerprint).where(OpportunityFingerprint.value_hash.in_(hashes)))
    ).scalars().all()
    wanted = set(pairs)
    matches: dict[uuid.UUID, set[str]] = {}
    for row in rows:
        if (row.kind, row.value_hash) in wanted:
            matches.setdefault(row.opportunity_id, set()).add(row.kind)
    return matches


async def lexical_candidates(
    db: AsyncSession, facts: DedupFacts, *, limit: int = LEXICAL_SCAN_LIMIT
) -> dict[uuid.UUID, float]:
    """Zero-cost deterministic candidate generator: token similarity of
    titles over the most recent `limit` opportunities."""
    if not facts.norm_title:
        return {}
    rows = (
        await db.execute(select(Opportunity.id, Opportunity.title).order_by(Opportunity.created_at.desc()).limit(limit))
    ).all()
    out: dict[uuid.UUID, float] = {}
    for opp_id, title in rows:
        score = title_similarity(facts.title, title)
        if score >= CANDIDATE_TITLE_SIMILARITY:
            out[opp_id] = score
    return out


SemanticSearch = Callable[[DedupFacts], Awaitable[dict[uuid.UUID, float]]]


async def load_existing_facts(db: AsyncSession, opportunity_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, DedupFacts]:
    ids = list(dict.fromkeys(opportunity_ids))
    if not ids:
        return {}
    opps = (await db.execute(select(Opportunity).where(Opportunity.id.in_(ids)))).scalars().all()
    extra = {
        row.opportunity_id: row
        for row in (
            await db.execute(select(AgentOpportunityFacts).where(AgentOpportunityFacts.opportunity_id.in_(ids)))
        ).scalars()
    }
    hashes: dict[uuid.UUID, str] = {}
    for row in (
        await db.execute(
            select(OpportunityFingerprint).where(
                OpportunityFingerprint.opportunity_id.in_(ids),
                OpportunityFingerprint.kind == FingerprintKind.CONTENT_HASH.value,
            )
        )
    ).scalars():
        hashes.setdefault(row.opportunity_id, row.value)
    result: dict[uuid.UUID, DedupFacts] = {}
    for opp in opps:
        facts_row = extra.get(opp.id)
        opp_type = opp.type.value if hasattr(opp.type, "value") else (str(opp.type) if opp.type else None)
        result[opp.id] = DedupFacts(
            title=opp.title,
            organization=facts_row.organization if facts_row else None,
            opportunity_type=opp_type,
            deadline=parse_date(opp.deadline),
            application_url=facts_row.application_url if facts_row else None,
            official_url=facts_row.official_url if facts_row else None,
            target_url=opp.target_url,
            funding=opp.funding_details,
            eligibility=opp.eligibility_criteria,
            content_hash=hashes.get(opp.id),
        )
    return result


async def decide(
    db: AsyncSession,
    facts: DedupFacts,
    *,
    semantic_search: SemanticSearch | None = None,
    extra_candidate_ids: Sequence[uuid.UUID] = (),
    exclude_ids: Iterable[uuid.UUID] = (),
) -> DedupDecision:
    """Full decision for one new opportunity: gather candidates from every
    layer, then let `compare()` judge each one deterministically."""
    excluded = set(exclude_ids)
    exact = await exact_matches(db, facts)
    lexical = await lexical_candidates(db, facts)
    semantic: dict[uuid.UUID, float] = {}
    if semantic_search is not None:
        semantic = await semantic_search(facts)

    candidate_ids = [
        cid
        for cid in dict.fromkeys([*exact.keys(), *extra_candidate_ids, *semantic.keys(), *lexical.keys()])
        if cid not in excluded
    ]
    existing = await load_existing_facts(db, candidate_ids)

    best: tuple[DedupOutcome, uuid.UUID | None, list[str]] = (DedupOutcome.NEW, None, ["no candidate matched"])
    report: list[dict[str, Any]] = []
    for cid in candidate_ids:
        other = existing.get(cid)
        if other is None:
            continue
        comparison = compare(facts, other)
        report.append(
            {
                "opportunity_id": str(cid),
                "outcome": comparison.outcome.value,
                "exact_layers": sorted(exact.get(cid, set())),
                "semantic_score": round(semantic[cid], 4) if cid in semantic else None,
                "lexical_score": round(lexical[cid], 4) if cid in lexical else None,
                "reasons": comparison.reasons,
                "signals": comparison.signals,
            }
        )
        if _OUTCOME_RANK[comparison.outcome] > _OUTCOME_RANK[best[0]]:
            best = (comparison.outcome, cid, comparison.reasons)
    return DedupDecision(outcome=best[0], opportunity_id=best[1], reasons=best[2], candidates=report)


# --------------------------------------------------------------------------
# Pre-extraction check (saves an LLM call on obvious re-posts)
# --------------------------------------------------------------------------

async def pre_extraction_duplicate(
    db: AsyncSession, *, links: Sequence[str], text: str, content_hash: str | None
) -> DedupDecision | None:
    """Before spending an LLM call: if the message's content hash, or one of
    its SPECIFIC links, already identifies an opportunity AND every full date
    in the message is already known for it, the message adds nothing new ->
    DUPLICATE. Any new date (e.g. an extended deadline) or any doubt ->
    ``None`` and the message goes through extraction + `decide()`."""
    DedupFacts(content_hash=content_hash)
    url_keys = [k for k in (normalize_url(link) for link in links) if k and is_specific_url(k)]
    pairs = [(FingerprintKind.CONTENT_HASH.value, sha256_hex(content_hash))] if content_hash else []
    for key in url_keys:
        pairs.append((FingerprintKind.URL.value, sha256_hex(key)))
        pairs.append((FingerprintKind.APPLICATION_URL.value, sha256_hex(key)))
    if not pairs:
        return None
    rows = (
        await db.execute(
            select(OpportunityFingerprint).where(OpportunityFingerprint.value_hash.in_({h for _, h in pairs}))
        )
    ).scalars().all()
    hits = [row for row in rows if (row.kind, row.value_hash) in set(pairs)]
    if not hits:
        return None
    by_opp: dict[uuid.UUID, set[str]] = {}
    for row in hits:
        by_opp.setdefault(row.opportunity_id, set()).add(row.kind)
    if len(by_opp) != 1:
        return None  # links point at several known opportunities -> let extraction sort it out
    (opp_id, kinds), = by_opp.items()
    if FingerprintKind.CONTENT_HASH.value in kinds:
        return DedupDecision(DedupOutcome.DUPLICATE, opp_id, ["identical content already processed"], [])
    known = (await load_existing_facts(db, [opp_id])).get(opp_id)
    message_dates = set(find_dates(text))
    known_dates = {known.deadline} if known and known.deadline else set()
    if message_dates and not message_dates <= known_dates:
        return None  # new date information -> may be an update
    if not message_dates and not known_dates:
        return None  # nothing to confirm with; be conservative
    return DedupDecision(
        DedupOutcome.DUPLICATE,
        opp_id,
        ["same specific URL as a known opportunity and no new dates"],
        [{"opportunity_id": str(opp_id), "exact_layers": sorted(kinds)}],
    )
