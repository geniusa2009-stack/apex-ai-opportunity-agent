"""D6: Evidence & Verification Intelligence.

Truth model: Claim -> Evidence -> Source -> Retrieval Timestamp ->
Verification Decision. This module is the ONE place that turns
candidate claims/evidence (which may come from an LLM) into a
deterministic verification decision. An LLM never chooses
`EvidenceStatus`/`VerificationDecisionStatus` directly -- see each
function's own docstring for the specific rule it enforces from the
D6 research constitution (spec section 0).

STEP 1/2 GROUND TRUTH (see this repo's own D6 audit): the only real
write site for `Opportunity.is_verified = True` is
`app/workers/tasks.py::_verify_new_opportunities`. This module supplies
the deterministic decision that site now requires before it may set
that flag -- replacing "AI said is_legitimate + confidence_score
cleared a threshold" alone with "a real evidence-backed
VerificationDecisionStatus.VERIFIED".
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import logging
import re
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.enums import EvidenceStatus, SourceAuthority, VerificationDecisionStatus
from app.models.evidence import Evidence
from app.models.source import Source

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Claim vocabulary -- built around the REAL fields
# `app/services/ai.py::VerifiedOpportunitySchema` extracts today, plus
# `country` (D5.1 `country_hint`) and `application_url`
# (`Opportunity.target_url`). Deliberately NOT the spec's full ~15-entry
# example list -- this codebase's extractor does not produce
# nationality_requirement/application_status/travel_support/
# program_dates/official_source data today, so claim types for those
# would have no real evidence path and would only create the illusion
# of coverage. New claim types are additive here once real extraction
# for them exists.
# ---------------------------------------------------------------------
ALLOWED_CLAIM_TYPES: frozenset[str] = frozenset(
    {
        "title",
        "organization",
        "opportunity_type",
        "country",
        "eligibility",
        "deadline",
        "funding",
        "application_url",
    }
)

# Claim types where stale evidence must NOT be treated as still-current
# proof -- "old evidence cannot automatically prove current status"
# (constitution rule 6). `title`/`organization` change rarely enough
# that staleness is not meaningfully evaluated for them.
_STALENESS_SENSITIVE_CLAIM_TYPES: frozenset[str] = frozenset({"deadline", "application_url", "eligibility", "funding"})

# How old evidence (by published_at, falling back to retrieved_at) may
# be before it stops counting as "current" for a staleness-sensitive
# claim. A deliberately simple, documented threshold -- full temporal
# reasoning is D8's job (spec section 12), not D6's.
STALE_EVIDENCE_MAX_AGE_DAYS = 365

_AUTHORITATIVE_TIERS: frozenset[SourceAuthority] = frozenset(
    {SourceAuthority.OFFICIAL, SourceAuthority.GOVERNMENT, SourceAuthority.UNIVERSITY}
)

# A small, explicitly-labeled allowlist of real, well-known third-party
# aggregator/institution domains this codebase's own scraper/aggregator
# already treats as real sources (see `app/scraper` spider configs /
# `app/services/aggregator_service.py`). NOT a claim that these are
# authoritative for any specific opportunity's own official eligibility
# -- only that they are known, non-anonymous aggregators, one notch
# above a wholly unknown domain.
_KNOWN_THIRD_PARTY_DOMAINS: frozenset[str] = frozenset(
    {
        "opportunitydesk.org",
        "scholars4dev.com",
        "youthop.com",
    }
)

_GOV_SUFFIX_RE = re.compile(r"(^|\.)gov(\.[a-z]{2})?$")
_EDU_SUFFIX_RE = re.compile(r"(^|\.)(edu|ac)(\.[a-z]{2})?$")


def _normalize_url(url: str) -> str:
    return url.strip().lower().rstrip("/")


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def classify_source_authority(url: str | None) -> SourceAuthority:
    """Deterministic, domain-pattern-only source authority classifier.

    DISCLOSED LIMITATION (spec section 6, honored plainly): this is the
    smallest safe version, not a real source-authority intelligence
    system. It cannot determine whether a domain structurally belongs to
    a SPECIFIC opportunity's own issuing organization (this codebase has
    no such registry), so `OFFICIAL` is never assigned automatically --
    only ever set explicitly by a caller that has independent proof
    (e.g. a human moderator marking a source as the opportunity's own
    official page). Never hardcode a `TRUSTED_THIRD_PARTY`/`OFFICIAL`
    result without an actual match below.
    """
    if not url:
        return SourceAuthority.UNKNOWN
    try:
        parsed = urlparse(url if "//" in url else f"//{url}")
        host = (parsed.netloc or parsed.path).split(":")[0].lower().strip()
    except Exception:
        return SourceAuthority.UNKNOWN
    if not host:
        return SourceAuthority.UNKNOWN

    labels = host.split(".")
    tail = ".".join(labels[-2:]) if len(labels) >= 2 else host

    if _GOV_SUFFIX_RE.search(host) or tail.endswith("gov"):
        return SourceAuthority.GOVERNMENT
    if _EDU_SUFFIX_RE.search(host):
        return SourceAuthority.UNIVERSITY
    if host in _KNOWN_THIRD_PARTY_DOMAINS or any(host.endswith(f".{d}") for d in _KNOWN_THIRD_PARTY_DOMAINS):
        return SourceAuthority.TRUSTED_THIRD_PARTY
    return SourceAuthority.UNKNOWN


def compute_evidence_hash(source_url: str | None, evidence_text: str, claim_type: str) -> str:
    """Stable fingerprint: sha256(normalized_source_url +
    normalized_evidence_text + claim_type). Used for dedup/change
    detection -- never just a DB id (spec section 13)."""
    normalized_url = _normalize_url(source_url) if source_url else ""
    payload = f"{normalized_url}|{_normalize_text(evidence_text)}|{claim_type.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


class EvidenceRejectedError(ValueError):
    """Raised when candidate evidence fails backend validation --
    e.g. no real source, a malformed/invented URL, or a disallowed
    claim type. Callers (the D6 pipeline in `tasks.py`) must catch this
    and simply not record the evidence -- never fabricate a fallback."""


async def get_or_create_claim(
    db: AsyncSession,
    *,
    opportunity_id,
    claim_type: str,
    claim_value: str,
    normalized_value: str | None = None,
    importance: str = "normal",
) -> Claim:
    """Idempotent claim lookup/creation for one opportunity+claim_type.
    Rejects a claim_type outside `ALLOWED_CLAIM_TYPES` -- "claim type is
    allowed" validation from spec section 7."""
    if claim_type not in ALLOWED_CLAIM_TYPES:
        raise EvidenceRejectedError(f"claim_type {claim_type!r} is not in ALLOWED_CLAIM_TYPES")

    existing = await db.execute(
        select(Claim).where(Claim.opportunity_id == opportunity_id, Claim.claim_type == claim_type)
    )
    claim = existing.scalar_one_or_none()
    if claim is not None:
        if claim_value and claim.claim_value != claim_value:
            # A newer extraction changed the raw value -- update the claim's
            # own current value (the claim row itself is a live pointer to
            # "what we currently believe"; the append-only history lives in
            # `Evidence`, never in this row).
            claim.claim_value = claim_value
            claim.normalized_value = normalized_value
        return claim

    claim = Claim(
        opportunity_id=opportunity_id,
        claim_type=claim_type,
        claim_value=claim_value,
        normalized_value=normalized_value,
        importance=importance,
        status=EvidenceStatus.UNKNOWN.value,
    )
    db.add(claim)
    await db.flush()
    return claim


async def record_evidence(
    db: AsyncSession,
    *,
    opportunity_id,
    claim_id,
    evidence_text: str,
    source_id=None,
    source_url: str | None = None,
    source_type: str | None = None,
    extraction_method: str = "manual",
    retrieval_method: str = "automated",
    supports_claim: bool | None = None,
    published_at: dt.datetime | None = None,
    retrieved_at: dt.datetime | None = None,
    evidence_metadata: dict | None = None,
) -> Evidence:
    """Validate and durably record one real piece of evidence.

    Enforces spec section 7's real backend validation of LLM-proposed
    evidence: (1) a real source must exist -- either `source_id`
    resolves to a real `Source` row, or `source_url` is a well-formed
    http(s) URL; a bare unsourced claim is rejected outright
    (constitution rule 1, rule 8's "reject an invented URL"). (2) the
    claim must already exist (FK). (3) evidence is deduplicated by
    `evidence_hash` -- identical evidence for the same claim returns the
    existing row rather than inserting a duplicate (spec section 18).
    Evidence rows are NEVER updated once created (see `evidence.py`'s
    module docstring) -- this function only ever SELECTs-then-INSERTs.
    """
    claim = await db.get(Claim, claim_id)
    if claim is None or claim.opportunity_id != opportunity_id:
        raise EvidenceRejectedError("evidence must reference an existing claim belonging to this opportunity")

    resolved_source: Source | None = None
    if source_id is not None:
        resolved_source = await db.get(Source, source_id)
        if resolved_source is None:
            raise EvidenceRejectedError(f"source_id {source_id!r} does not resolve to a real Source row")

    if resolved_source is None and not source_url:
        raise EvidenceRejectedError("evidence requires a real source_id or a source_url -- no source, no claim")

    if source_url and not _is_valid_url(source_url):
        raise EvidenceRejectedError(f"source_url {source_url!r} is not a well-formed http(s) URL -- rejected")

    if not evidence_text or not evidence_text.strip():
        raise EvidenceRejectedError("evidence_text must be non-empty")

    effective_url = source_url or (resolved_source.base_url if resolved_source else None)
    evidence_hash = compute_evidence_hash(effective_url, evidence_text, claim.claim_type)

    existing = await db.execute(
        select(Evidence).where(Evidence.claim_id == claim_id, Evidence.evidence_hash == evidence_hash)
    )
    duplicate = existing.scalar_one_or_none()
    if duplicate is not None:
        logger.info("D6: duplicate evidence (hash=%s) for claim %s -- returning existing row", evidence_hash, claim_id)
        return duplicate

    authority = classify_source_authority(effective_url)
    resolved_source_type = source_type or (resolved_source.source_type if resolved_source else None)

    evidence = Evidence(
        opportunity_id=opportunity_id,
        claim_id=claim_id,
        source_id=source_id,
        source_url=source_url,
        source_type=resolved_source_type,
        source_authority=authority.value,
        evidence_text=evidence_text,
        evidence_hash=evidence_hash,
        retrieved_at=retrieved_at or dt.datetime.now(dt.timezone.utc),
        published_at=published_at,
        supports_claim=supports_claim,
        extraction_method=extraction_method,
        retrieval_method=retrieval_method,
        evidence_metadata=evidence_metadata,
    )
    db.add(evidence)
    await db.flush()
    return evidence


@dataclasses.dataclass
class ClaimEvaluation:
    claim_id: object
    claim_type: str
    status: EvidenceStatus
    supporting_evidence_ids: list
    contradicting_evidence_ids: list
    reason: str


def _is_current(evidence: Evidence, *, now: dt.datetime) -> bool:
    reference = evidence.published_at or evidence.retrieved_at
    if reference is None:
        return True
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=dt.timezone.utc)
    age_days = (now - reference).days
    return age_days <= STALE_EVIDENCE_MAX_AGE_DAYS


async def _evaluate_claim_pure(db: AsyncSession, *, claim: Claim, now: dt.datetime | None = None) -> ClaimEvaluation:
    """Deterministic evaluation of one claim's real `Evidence` rows.

    Rules (spec section 9, constitution rules 1/5/6):
    - No evidence at all -> UNSUPPORTED ("no source -> no factual claim").
    - Authoritative support AND authoritative contradiction -> CONFLICTED
      ("conflicting authoritative evidence -> HOLD, never silently choose").
    - Any contradiction (no authoritative support to outweigh it) ->
      CONTRADICTED.
    - Support exists, no contradiction, but staleness-sensitive claim
      type and every supporting item is stale -> STALE ("old evidence
      cannot automatically prove current status").
    - Support exists, no contradiction, current -> SUPPORTED.
    - Evidence exists but none of it takes a support/contradict position
      -> UNKNOWN.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    result = await db.execute(select(Evidence).where(Evidence.claim_id == claim.id))
    evidence_rows: list[Evidence] = list(result.scalars().all())

    if not evidence_rows:
        return ClaimEvaluation(
            claim_id=claim.id,
            claim_type=claim.claim_type,
            status=EvidenceStatus.UNSUPPORTED,
            supporting_evidence_ids=[],
            contradicting_evidence_ids=[],
            reason=f"No evidence recorded for claim '{claim.claim_type}'.",
        )

    supporting = [e for e in evidence_rows if e.supports_claim is True]
    contradicting = [e for e in evidence_rows if e.supports_claim is False]

    authoritative_support = [e for e in supporting if SourceAuthority(e.source_authority) in _AUTHORITATIVE_TIERS]
    authoritative_contradict = [e for e in contradicting if SourceAuthority(e.source_authority) in _AUTHORITATIVE_TIERS]

    supporting_ids = [e.id for e in supporting]
    contradicting_ids = [e.id for e in contradicting]

    if authoritative_support and authoritative_contradict:
        return ClaimEvaluation(
            claim.id, claim.claim_type, EvidenceStatus.CONFLICTED, supporting_ids, contradicting_ids,
            reason=(
                f"Claim '{claim.claim_type}' has {len(authoritative_support)} authoritative supporting "
                f"and {len(authoritative_contradict)} authoritative contradicting evidence item(s) -- HOLD."
            ),
        )

    if contradicting and not supporting:
        return ClaimEvaluation(
            claim.id, claim.claim_type, EvidenceStatus.CONTRADICTED, supporting_ids, contradicting_ids,
            reason=f"Claim '{claim.claim_type}' is contradicted by {len(contradicting)} evidence item(s).",
        )

    if contradicting and supporting and not authoritative_support:
        # Non-authoritative sources disagree with each other and no
        # authoritative evidence exists either way -- do not silently
        # pick a side.
        return ClaimEvaluation(
            claim.id, claim.claim_type, EvidenceStatus.CONFLICTED, supporting_ids, contradicting_ids,
            reason=f"Claim '{claim.claim_type}' has conflicting non-authoritative evidence and no authoritative source.",
        )

    if supporting:
        if claim.claim_type in _STALENESS_SENSITIVE_CLAIM_TYPES and not any(
            _is_current(e, now=now) for e in supporting
        ):
            return ClaimEvaluation(
                claim.id, claim.claim_type, EvidenceStatus.STALE, supporting_ids, contradicting_ids,
                reason=f"Claim '{claim.claim_type}' is only supported by evidence older than {STALE_EVIDENCE_MAX_AGE_DAYS} days.",
            )
        return ClaimEvaluation(
            claim.id, claim.claim_type, EvidenceStatus.SUPPORTED, supporting_ids, contradicting_ids,
            reason=f"Claim '{claim.claim_type}' is supported by {len(supporting)} evidence item(s).",
        )

    return ClaimEvaluation(
        claim.id, claim.claim_type, EvidenceStatus.UNKNOWN, supporting_ids, contradicting_ids,
        reason=f"Claim '{claim.claim_type}' has evidence, but none of it confirms or denies the claim.",
    )



async def evaluate_claim(db: AsyncSession, *, claim: Claim, now: dt.datetime | None = None) -> ClaimEvaluation:
    """Public entry point: runs the pure evaluator above and PERSISTS the
    resulting `EvidenceStatus` onto `claim.status` -- this is the one
    real place `Claim.status` is ever written, always backend-derived,
    never LLM-chosen (constitution rule 3)."""
    evaluation = await _evaluate_claim_pure(db, claim=claim, now=now)
    claim.status = evaluation.status.value
    return evaluation


@dataclasses.dataclass
class VerificationDecision:
    status: VerificationDecisionStatus
    reason: str
    evaluated_claims: list
    supporting_evidence_ids: list
    contradicting_evidence_ids: list
    evaluated_at: dt.datetime
    verification_version: str = "d6.1"


async def decide_verification(
    db: AsyncSession, *, opportunity_id, claims: list[Claim], now: dt.datetime | None = None
) -> VerificationDecision:
    """Deterministic aggregate verification decision from real,
    per-claim evaluations. Never LLM-generated -- the reason string is
    built purely from real evaluated claims (spec section 14).

    - Any claim CONFLICTED -> HOLD (constitution rule 5: never silently
      choose between conflicting authoritative evidence).
    - No claims at all, or every claim UNSUPPORTED/UNKNOWN -> UNVERIFIED
      (constitution rule 2: no evidence -> never Verified).
    - Any staleness-sensitive claim STALE (and none CONFLICTED) -> STALE.
    - Any claim CONTRADICTED (and none CONFLICTED) -> UNVERIFIED.
    - At least one claim SUPPORTED, and every claim is
      SUPPORTED/UNKNOWN (none CONTRADICTED/CONFLICTED/STALE) -> VERIFIED.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    evaluations = [await evaluate_claim(db, claim=c, now=now) for c in claims]

    all_supporting: list = []
    all_contradicting: list = []
    for ev in evaluations:
        all_supporting.extend(ev.supporting_evidence_ids)
        all_contradicting.extend(ev.contradicting_evidence_ids)

    reasons = [ev.reason for ev in evaluations]

    if not evaluations:
        return VerificationDecision(
            VerificationDecisionStatus.UNVERIFIED,
            "No claims were evaluated -- no evidence, no verification (constitution rule 2).",
            evaluations, all_supporting, all_contradicting, now,
        )

    if any(ev.status == EvidenceStatus.CONFLICTED for ev in evaluations):
        return VerificationDecision(
            VerificationDecisionStatus.HOLD,
            "HOLD: at least one claim has conflicting authoritative evidence. " + " ".join(reasons),
            evaluations, all_supporting, all_contradicting, now,
        )

    if any(ev.status == EvidenceStatus.STALE for ev in evaluations):
        return VerificationDecision(
            VerificationDecisionStatus.STALE,
            "STALE: at least one claim is only supported by outdated evidence. " + " ".join(reasons),
            evaluations, all_supporting, all_contradicting, now,
        )

    if any(ev.status == EvidenceStatus.CONTRADICTED for ev in evaluations):
        return VerificationDecision(
            VerificationDecisionStatus.UNVERIFIED,
            "UNVERIFIED: at least one claim is contradicted by evidence. " + " ".join(reasons),
            evaluations, all_supporting, all_contradicting, now,
        )

    if any(ev.status == EvidenceStatus.SUPPORTED for ev in evaluations):
        return VerificationDecision(
            VerificationDecisionStatus.VERIFIED,
            "VERIFIED: all evaluated claims are supported or unknown, none contradicted/conflicted/stale, "
            "and at least one claim has real supporting evidence. " + " ".join(reasons),
            evaluations, all_supporting, all_contradicting, now,
        )

    return VerificationDecision(
        VerificationDecisionStatus.UNVERIFIED,
        "UNVERIFIED: no claim has real supporting evidence. " + " ".join(reasons),
        evaluations, all_supporting, all_contradicting, now,
    )
