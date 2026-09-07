"""D6 (Evidence & Verification Intelligence) admin read schemas.

Completes D6's own originally-scoped, then-deferred admin surface: read
-only response shapes for the three new `GET /admin/opportunities/{id}
/evidence|claims|verification` endpoints and the one new
`GET /admin/opportunities/evidence-stats` observability endpoint (see
`app/api/v1/endpoints/admin_opportunities.py`'s own D6 section for the
route bodies, and `app/services/evidence_verification.py` for the real
`Claim`/`Evidence`/`VerificationDecision` shapes these mirror).

DISCLOSED OMISSION: `Evidence.evidence_metadata` (JSONB) is deliberately
NOT exposed on `EvidenceOut`. As of this pass, the one real production
write path (`app/workers/tasks.py::_run_d6_evidence_pipeline`) never
sets it (always `None`) -- but `record_evidence`'s own signature accepts
an arbitrary caller-supplied `dict`, and a future manual/API evidence-
entry path could put anything in it. Since this is an admin-only,
read-only surface and there's no current real content to justify the
exposure risk, it's left out entirely rather than exposed "because it's
probably fine today."
"""

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict


class EvidenceOut(BaseModel):
    """One real, persisted `Evidence` row -- never a fabricated/derived
    shape. See this module's own docstring for why `evidence_metadata`
    is not included."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    opportunity_id: uuid.UUID
    claim_id: uuid.UUID
    source_id: uuid.UUID | None
    source_url: str | None
    source_type: str | None
    source_authority: str
    evidence_text: str
    evidence_hash: str
    retrieved_at: dt.datetime
    published_at: dt.datetime | None
    valid_from: dt.datetime | None
    valid_until: dt.datetime | None
    supports_claim: bool | None
    extraction_method: str
    retrieval_method: str
    created_at: dt.datetime


class ClaimOut(BaseModel):
    """One real, persisted `Claim` row, plus a real `evidence_count` --
    a genuine `COUNT(*)` from the `evidence` table
    (`app/api/v1/endpoints/admin_opportunities.py::get_opportunity_claims`),
    never fabricated or estimated."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    opportunity_id: uuid.UUID
    claim_type: str
    claim_value: str
    normalized_value: str | None
    status: str
    importance: str
    created_at: dt.datetime
    updated_at: dt.datetime
    evidence_count: int


class EvaluatedClaimOut(BaseModel):
    """One claim's real evaluation outcome, as produced by
    `evidence_verification._evaluate_claim_pure`/`evaluate_claim` --
    mirrors that module's own `ClaimEvaluation` dataclass."""

    claim_id: uuid.UUID
    claim_type: str
    status: str
    reason: str
    supporting_evidence_ids: list[uuid.UUID]
    contradicting_evidence_ids: list[uuid.UUID]


class VerificationDecisionOut(BaseModel):
    """The real, current verification decision for one opportunity --
    reconstructed READ-ONLY from that opportunity's real, already-
    persisted claims/evidence via
    `evidence_verification.evaluate_verification_readonly`. Mirrors
    `evidence_verification.VerificationDecision` exactly; this is a
    read/recompute-for-display endpoint, not a new decision path -- it
    can never itself flip `Opportunity.is_verified`."""

    status: str
    reason: str
    evaluated_claims: list[EvaluatedClaimOut]
    supporting_evidence_ids: list[uuid.UUID]
    contradicting_evidence_ids: list[uuid.UUID]
    evaluated_at: dt.datetime
    verification_version: str


class EvidenceStatsOut(BaseModel):
    """Real, freshly-computed DB aggregation counts -- the plain
    `admin_stats.py`/`moderation_stats_service.py`-style posture (no
    metrics library exists anywhere in this codebase), not application-
    incremented counters.

    DISCLOSED DERIVATION NOTES (see `caveats`, always populated when any
    field below is an approximation rather than an exact persisted-data
    count):

    - `evidence_duplicate_total` is `None`. `evidence_verification.
      record_evidence`'s dedup path (identical `evidence_hash` for a
      claim returns the existing row rather than inserting) has NO
      persisted signal at all for "a dedup hit occurred" -- it is a
      pure early-return with no counter, no log row, nothing queryable
      after the fact. Only the resulting DISTINCT `evidence_hash` rows
      that WERE inserted are visible in the table; how many redundant
      calls were made and silently short-circuited is not recoverable
      from stored data. Fabricating a number here would misrepresent
      what is actually knowable.
    - `claims_evaluated_total` counts claims with a non-default
      (`!= unknown`) `status` OR at least one linked `Evidence` row.
      This is exact for this codebase's one real write path (every
      claim is evaluated immediately after creation by
      `_run_d6_evidence_pipeline`), but is a "claim.status default is
      also literally UNKNOWN" ambiguity in the general case: a claim
      genuinely evaluated as UNKNOWN (evidence exists, but none of it
      confirms or denies the claim) with that evidence since removed is
      indistinguishable from "never evaluated." No such case exists in
      the current codebase (evidence rows are never deleted -- see
      `Evidence`'s own immutability docstring), so this is exact today.
    - `verification_verified_total` is the real, persisted
      `COUNT(*) WHERE Opportunity.is_verified = true` -- this is the
      authoritative production signal (the ONE place that flag is ever
      set to `True` requires a real `VerificationDecisionStatus.
      VERIFIED`, confirmed in D6's own commit).
    - `verification_hold_total`/`verification_unverified_total`/
      `verification_stale_total`/`verification_rejected_total` are
      LIVE-RECOMPUTED (via `evaluate_verification_readonly`, read-only,
      nothing written) over every currently-unverified opportunity that
      has at least one real claim -- `VerificationDecision` itself is
      never persisted anywhere queryable (only its `VERIFIED` outcome's
      side effect, `is_verified=True`, is), so there is no cheaper,
      already-persisted-data query available for this breakdown.
    """

    evidence_created_total: int
    evidence_duplicate_total: int | None
    claims_evaluated_total: int
    claims_supported_total: int
    claims_contradicted_total: int
    claims_stale_total: int
    claims_unsupported_total: int
    claims_conflicted_total: int
    claims_unknown_total: int
    verification_verified_total: int
    verification_hold_total: int
    verification_unverified_total: int
    verification_stale_total: int
    verification_rejected_total: int
    caveats: list[str]
