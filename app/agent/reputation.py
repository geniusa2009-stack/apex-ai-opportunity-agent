"""Source reputation.

`Source.trust_score` (0..1) influences two things only:

1. **Priority** -- which queued job a worker picks first.
2. **Required evidence** -- how much independent confirmation the verifier
   demands before the opportunity may reach the human approval queue
   without a "needs closer review" flag.

It NEVER skips verification and NEVER approves or publishes anything:
``TRUST != AUTOMATIC APPROVAL``. Every opportunity, from every source,
still goes through verification, the quality gate and a human.

Updates are driven only by *human* verdicts (approve / reject as wrong or
misleading), are bounded per event, and are clamped to [MIN, MAX] so one
bad day cannot zero a source and no source can reach "perfect".
"""

from __future__ import annotations

import enum

from app.models.enums import SourceType

MIN_TRUST = 0.05
MAX_TRUST = 0.95
NEUTRAL_TRUST = 0.5

# Starting trust per source type. Deliberately conservative: an "official"
# source starts high but is not 1.0; WhatsApp/unknown channels start neutral
# or below.
DEFAULT_TRUST_BY_TYPE: dict[str, float] = {
    SourceType.OFFICIAL.value: 0.8,
    SourceType.VOLUNTEER.value: 0.6,
    SourceType.WEBSITE.value: 0.5,
    SourceType.RSS.value: 0.5,
    SourceType.MANUAL.value: 0.6,
    SourceType.TELEGRAM.value: 0.4,
    SourceType.FACEBOOK.value: 0.4,
    SourceType.WHATSAPP.value: 0.4,
    SourceType.OTHER.value: 0.3,
}


class TrustTier(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def default_trust_for(source_type: str) -> float:
    return DEFAULT_TRUST_BY_TYPE.get(source_type, NEUTRAL_TRUST)


def tier_for(trust_score: float) -> TrustTier:
    if trust_score >= 0.75:
        return TrustTier.HIGH
    if trust_score >= 0.45:
        return TrustTier.MEDIUM
    return TrustTier.LOW


def job_priority(trust_score: float) -> int:
    """0..100, higher runs first."""
    return int(round(max(0.0, min(1.0, trust_score)) * 100))


def requires_official_confirmation(trust_score: float) -> bool:
    """LOW/MEDIUM sources need the official page to confirm key facts before
    their drafts are considered fully verified. HIGH sources still get every
    check -- the difference is only whether "source-only" evidence is flagged
    as a blocker or as a warning for the human reviewer."""
    return tier_for(trust_score) != TrustTier.HIGH


class TrustEvent(str, enum.Enum):
    HUMAN_APPROVED = "human_approved"  # a human approved a draft from this source
    HUMAN_REJECTED_INACCURATE = "human_rejected_inaccurate"  # rejected as wrong/misleading/scam
    VERIFICATION_CONTRADICTED = "verification_contradicted"  # official source contradicted it


_DELTAS: dict[TrustEvent, float] = {
    TrustEvent.HUMAN_APPROVED: +0.02,
    TrustEvent.HUMAN_REJECTED_INACCURATE: -0.08,
    TrustEvent.VERIFICATION_CONTRADICTED: -0.05,
}


def updated_trust(current: float, event: TrustEvent) -> float:
    """Bounded, asymmetric update (losing trust is faster than gaining it)."""
    value = current + _DELTAS[event]
    return round(max(MIN_TRUST, min(MAX_TRUST, value)), 4)
