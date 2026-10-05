"""Opportunity extraction.

1. `triage()` -- deterministic rule filter. Obvious chatter ("شكرا",
   "تمام 👍", a sticker caption) never reaches the LLM. Obvious and
   uncertain messages go to ONE extraction call, whose `is_opportunity`
   field doubles as the classifier (no separate classifier call).
2. `ExtractedOpportunity` -- the Pydantic contract. Besides types/ranges it
   enforces *grounding* against the source message through the validation
   context:
     * `application_url` / `official_url` must be links that literally
       appear in the message (normalized comparison) -- no invented links
     * `deadline` / `start_date` / `end_date` must be dates that appear in
       the message -- no invented or "estimated" dates
   A violation is sent back to the model by the self-correction loop; if it
   still cannot comply the job goes to NEEDS_REVIEW.
"""

from __future__ import annotations

import dataclasses
import enum
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from app.agent.dates import find_dates, parse_date
from app.agent.llm import LLMClient
from app.agent.normalization import is_valid_http_url, normalize_text, normalize_url
from app.models.enums import OpportunityType

NOT_STATED_AR = "غير موضح في المصدر الرسمي"

ALLOWED_TYPES: tuple[str, ...] = tuple(t.value for t in OpportunityType)

# --------------------------------------------------------------------------
# Deterministic triage
# --------------------------------------------------------------------------

_OPPORTUNITY_KEYWORDS = (
    # Arabic (normalized forms: ا for أ/إ, ه for ة, ي for ى)
    "منحه", "منح", "تدريب", "تدريبي", "مسابقه", "مسابقات", "فرصه", "فرص", "برنامج", "زماله", "مؤتمر",
    "تطوع", "متطوع", "كورس", "دوره", "ورشه", "هاكاثون", "التقديم", "للتقديم", "قدم", "سجل", "التسجيل",
    "اخر موعد", "ممول", "تبادل", "وظيفه", "تدريب صيفي", "جائزه", "معسكر",
    # English
    "scholarship", "internship", "fellowship", "competition", "hackathon", "conference", "volunteer",
    "course", "workshop", "program", "programme", "deadline", "apply", "application", "grant", "award",
    "exchange", "bootcamp", "summer school", "fully funded", "funded", "registration", "olympiad", "challenge",
)
_CHATTER = re.compile(
    r"^(?:\W|\d|ok|okay|thanks?|thank you|thx|good morning|hi|hello|hey|yes|no|"
    r"شكرا|تمام|ماشي|اوكي|حاضر|صباح الخير|مساء الخير|السلام عليكم|وعليكم السلام|الحمد لله|ان شاء الله|"
    r"جزاك الله خيرا|ربنا يبارك|مبروك|اهلا|نعم|لا)+$",
    re.I,
)


class TriageDecision(str, enum.Enum):
    OPPORTUNITY = "opportunity"
    UNCERTAIN = "uncertain"
    CHATTER = "chatter"


@dataclasses.dataclass(frozen=True)
class TriageResult:
    decision: TriageDecision
    reasons: tuple[str, ...]

    @property
    def needs_llm(self) -> bool:
        return self.decision != TriageDecision.CHATTER


def triage(text: str, links: list[str]) -> TriageResult:
    normalized = normalize_text(text)
    stripped = re.sub(r"\s+", " ", normalized).strip()
    keywords = [k for k in _OPPORTUNITY_KEYWORDS if k in stripped]
    has_link = bool(links)
    if not stripped and not has_link:
        return TriageResult(TriageDecision.CHATTER, ("empty",))
    if not has_link and (len(stripped) < 15 or _CHATTER.match(stripped.replace(" ", "")) or _CHATTER.match(stripped)):
        return TriageResult(TriageDecision.CHATTER, ("short or conversational message without links",))
    if keywords and has_link:
        return TriageResult(TriageDecision.OPPORTUNITY, (f"keywords {keywords[:3]} + link",))
    if keywords or has_link:
        return TriageResult(TriageDecision.UNCERTAIN, ("keyword without link" if keywords else "link without keyword",))
    if len(stripped) < 80:
        return TriageResult(TriageDecision.CHATTER, ("short message without opportunity signals",))
    return TriageResult(TriageDecision.UNCERTAIN, ("long message without explicit signals",))


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

class ExtractedOpportunity(BaseModel):
    """What the extractor must return. Unknown facts are null -- never guessed."""

    is_opportunity: bool = Field(description="false for chatter, ads for paid services, news without an application")
    not_opportunity_reason: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=300)
    organization: str | None = Field(default=None, max_length=300)
    opportunity_type: str | None = Field(default=None, description=f"one of {list(ALLOWED_TYPES)}")
    description: str | None = Field(default=None, max_length=2000)
    eligibility: str | None = Field(default=None, max_length=2000)
    age_min: int | None = Field(default=None, ge=5, le=100)
    age_max: int | None = Field(default=None, ge=5, le=100)
    country_requirement: str | None = Field(default=None, max_length=300, description="nationality/residency requirement")
    location: str | None = Field(default=None, max_length=300)
    is_online: bool | None = None
    funding: str | None = Field(default=None, max_length=1000)
    requires_payment: bool | None = Field(default=None, description="true if applicants must pay any fee")
    deadline: str | None = Field(default=None, description="YYYY-MM-DD, only if stated in the message")
    start_date: str | None = None
    end_date: str | None = None
    application_url: str | None = None
    official_url: str | None = None
    required_documents: list[str] = Field(default_factory=list, max_length=20)
    contact: str | None = Field(default=None, max_length=300)
    language: Literal["ar", "en", "fr", "other"] = "ar"
    confidence: float = Field(default=0.5, ge=0, le=1)
    missing_fields: list[str] = Field(default_factory=list, max_length=20)
    suspicious_signals: list[str] = Field(default_factory=list, max_length=10)

    # ---- field rules ---------------------------------------------------
    @field_validator("title", "organization", "description", "eligibility", "country_requirement", "location",
                     "funding", "contact", "not_opportunity_reason")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or value.lower() in {"null", "none", "n/a", "unknown", "غير معروف", NOT_STATED_AR}:
            return None
        return value

    @field_validator("opportunity_type")
    @classmethod
    def _known_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower().replace(" ", "_").replace("-", "_")
        if normalized not in ALLOWED_TYPES:
            raise ValueError(f"must be one of {list(ALLOWED_TYPES)} (got {value!r})")
        return normalized

    @field_validator("deadline", "start_date", "end_date")
    @classmethod
    def _grounded_date(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None or not str(value).strip() or str(value).strip().lower() == "null":
            return None
        parsed = parse_date(value)
        if parsed is None:
            raise ValueError("must be a single full date in YYYY-MM-DD format, or null")
        allowed = (info.context or {}).get("allowed_dates")
        if allowed is not None and parsed not in allowed:
            raise ValueError(
                f"{parsed.isoformat()} is not a date written in the message; use only dates stated in the text, otherwise null"
            )
        return parsed.isoformat()

    @field_validator("application_url", "official_url")
    @classmethod
    def _grounded_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None or not str(value).strip() or str(value).strip().lower() == "null":
            return None
        value = str(value).strip()
        if not is_valid_http_url(value):
            raise ValueError("must be a full http(s) URL or null")
        allowed = (info.context or {}).get("allowed_urls")
        if allowed is not None and normalize_url(value) not in allowed:
            raise ValueError("must be one of the links that appear in the message (copy it exactly), or null")
        return value

    @field_validator("required_documents", "missing_fields", "suspicious_signals")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        return [v.strip()[:200] for v in value if isinstance(v, str) and v.strip()]

    @model_validator(mode="after")
    def _consistency(self) -> "ExtractedOpportunity":
        if self.is_opportunity:
            if not self.title:
                raise ValueError("title is required when is_opportunity is true")
            if not self.opportunity_type:
                raise ValueError("opportunity_type is required when is_opportunity is true (use 'other' if unclear)")
        if self.age_min is not None and self.age_max is not None and self.age_min > self.age_max:
            raise ValueError("age_min must be <= age_max")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be <= end_date")
        return self

    # ---- helpers ---------------------------------------------------------
    @property
    def deadline_date(self):
        return parse_date(self.deadline)

    def computed_missing_fields(self) -> list[str]:
        important = ("organization", "deadline", "application_url", "eligibility", "funding")
        return [name for name in important if getattr(self, name) in (None, "", [])]


# --------------------------------------------------------------------------
# Extraction call
# --------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = f"""\
You are the APEX opportunity extractor. APEX shares legitimate opportunities \
(scholarships, internships, competitions, training, volunteering, conferences, \
fellowships, courses) with Arab and Egyptian students.

Read ONE message and return ONE JSON object with exactly these keys:
is_opportunity, not_opportunity_reason, title, organization, opportunity_type, \
description, eligibility, age_min, age_max, country_requirement, location, \
is_online, funding, requires_payment, deadline, start_date, end_date, \
application_url, official_url, required_documents, contact, language, \
confidence, missing_fields, suspicious_signals.

Hard rules:
1. Never invent facts. If the message does not state something, use null \
(or [] for lists). Do not "estimate" deadlines, ages, amounts or links.
2. application_url and official_url MUST be copied from the links listed in \
the message, or null.
3. deadline/start_date/end_date MUST be dates written in the message, \
formatted YYYY-MM-DD, or null. Relative dates ("next Friday") -> null.
4. opportunity_type is one of: {", ".join(ALLOWED_TYPES)}.
5. is_opportunity=false for chatter, greetings, paid advertising, or news \
with no way to apply; then explain in not_opportunity_reason.
6. requires_payment=true if applicants must pay any fee to apply/participate.
7. suspicious_signals: list concrete red flags you see (e.g. "asks for a \
fee via mobile wallet", "guaranteed acceptance", "only a phone number to \
apply"). Do not judge, just list.
8. title: short and factual, in the message's main language.
9. Output JSON only. No markdown, no commentary.
"""


def build_extraction_prompt(text: str, links: list[str], *, source_hint: str | None = None) -> str:
    link_lines = "\n".join(f"- {link}" for link in links) or "(no links)"
    hint = f"\nSource: {source_hint}" if source_hint else ""
    return f"Message:\n<<<\n{text}\n>>>\n\nLinks found in the message:\n{link_lines}{hint}"


def grounding_context(text: str, links: list[str]) -> dict[str, Any]:
    allowed_urls = {key for key in (normalize_url(link) for link in links) if key}
    return {"allowed_urls": allowed_urls, "allowed_dates": set(find_dates(text))}


async def extract_opportunity(
    client: LLMClient, *, text: str, links: list[str], source_hint: str | None = None
):
    """One LLM call (+ bounded self-correction). Returns `StructuredResult`."""
    return await client.structured(
        system=EXTRACTION_SYSTEM_PROMPT,
        user=build_extraction_prompt(text, links, source_hint=source_hint),
        schema=ExtractedOpportunity,
        stage="extraction",
        validation_context=grounding_context(text, links),
    )
