"""Enums for the APEX AI Opportunity Agent (app/agent/).

All of them back plain String columns (not native Postgres ENUM types), the
same posture `Source.source_type` already uses: adding a value later is a
code change only, never an `ALTER TYPE` migration.
"""

import enum


class SourceAccessStatus(str, enum.Enum):
    """Whether APEX is allowed to ingest from a source at all. Only
    APPROVED sources enter the pipeline; trust never overrides this."""

    APPROVED = "approved"
    PENDING = "pending"
    REVOKED = "revoked"


class SourceMessageStatus(str, enum.Enum):
    RECEIVED = "received"
    QUEUED = "queued"
    DUPLICATE_MESSAGE = "duplicate_message"  # same message already ingested
    NOT_OPPORTUNITY = "not_opportunity"  # rule filter or extractor said no
    PROCESSED = "processed"
    FAILED = "failed"
    REJECTED_SOURCE = "rejected_source"  # source not approved / not allowlisted


class PipelineJobType(str, enum.Enum):
    PROCESS_MESSAGE = "process_message"
    PUBLISH = "publish"


class PipelineJobStatus(str, enum.Enum):
    QUEUED = "queued"
    PROCESSING = "processing"  # normalization + exact/semantic dedup
    EXTRACTING = "extracting"
    VERIFYING = "verifying"
    WRITING = "writing"
    REVIEWING = "reviewing"
    WAITING_APPROVAL = "waiting_approval"
    NEEDS_REVIEW = "needs_review"  # paused for a human (data/validation problem)
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class JobOutcome(str, enum.Enum):
    """Why a job reached COMPLETED (or a terminal state)."""

    DRAFT_READY = "draft_ready"
    PUBLISHED = "published"
    DUPLICATE = "duplicate"
    UPDATED_EXISTING = "updated_existing"
    NOT_OPPORTUNITY = "not_opportunity"
    REJECTED_BY_REVIEW = "rejected_by_review"
    REJECTED_BY_VERIFICATION = "rejected_by_verification"
    REJECTED_BY_HUMAN = "rejected_by_human"
    EXPIRED = "expired"


class DedupOutcome(str, enum.Enum):
    NEW = "new"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    DUPLICATE = "duplicate"
    UPDATE_EXISTING = "update_existing"


class FingerprintKind(str, enum.Enum):
    URL = "url"  # normalized target/official URL
    APPLICATION_URL = "application_url"
    TITLE = "title"  # normalized title
    ORG_TITLE = "org_title"  # normalized organization + title
    CONTENT_HASH = "content_hash"


class VerificationLevel(str, enum.Enum):
    """Agent-level summary on top of the D6 decision.

    OFFICIAL_CONFIRMED: the official/application page was fetched and
        independently confirmed the key facts.
    SOURCE_ONLY: only the incoming message supports the facts.
    CONTRADICTED: official page or evidence contradicts a fact.
    REJECTED: deterministic red flags (expired, scam patterns, invalid URL).
    """

    OFFICIAL_CONFIRMED = "official_confirmed"
    PARTIALLY_CONFIRMED = "partially_confirmed"
    SOURCE_ONLY = "source_only"
    CONTRADICTED = "contradicted"
    REJECTED = "rejected"


class ReviewDecision(str, enum.Enum):
    APPROVE = "approve"
    NEEDS_REVISION = "needs_revision"
    REJECT = "reject"


class DraftStatus(str, enum.Enum):
    DRAFT = "draft"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISION_REQUESTED = "revision_requested"
    SUPERSEDED = "superseded"
    PUBLISHED = "published"


class PublicationStatus(str, enum.Enum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    # Manual publisher: the post text is ready; a team member posts it (e.g.
    # in a WhatsApp community) and confirms with the link -> PUBLISHED.
    AWAITING_MANUAL = "awaiting_manual"
    # A previous attempt may or may not have reached the platform (crash
    # between "send" and "record"). Never auto-republished: a human or the
    # publisher's own lookup must resolve it. At-most-once by design.
    UNKNOWN = "unknown"
