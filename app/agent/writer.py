"""APEX Writer.

Turns verified facts into the standard APEX post. Rule: **VERIFIED FACTS >
HISTORICAL STYLE** -- examples may shape tone/structure, never content.

Two modes (AGENT_WRITER_MODE):

* ``template`` (default, zero AI cost) -- deterministic Arabic APEX format.
  Missing key facts are written as "غير موضح في المصدر الرسمي", never
  guessed.
* ``llm`` -- one LLM call for a more natural post, then the deterministic
  `fact_guard` checks every URL, date and number in the text against the
  facts. A violation is fed back once per allowed round; if the model still
  cannot comply, the template post is used instead (and that is recorded).

`fact_guard` is also run by the Reviewer on every draft, whatever wrote it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from typing import Any

from pydantic import BaseModel, Field

from app.agent.dates import find_dates
from app.agent.extraction import NOT_STATED_AR, ExtractedOpportunity
from app.agent.llm import LLMClient
from app.agent.normalization import extract_links, normalize_text, normalize_url
from app.agent.observability import log_stage

APEX_SIGNATURE = "— APEX | فرص للطلاب العرب"

TYPE_LABELS_AR: dict[str, str] = {
    "scholarship": "منحة دراسية",
    "fellowship": "زمالة",
    "grant": "منحة تمويل",
    "research_funding": "تمويل بحثي",
    "research_program": "برنامج بحثي",
    "research_internship": "تدريب بحثي",
    "phd": "دكتوراه",
    "masters": "ماجستير",
    "bachelors": "بكالوريوس",
    "internship": "تدريب",
    "company_training": "تدريب في شركة",
    "job": "وظيفة",
    "competition": "مسابقة",
    "accelerator": "مسرّعة أعمال",
    "incubator": "حاضنة أعمال",
    "startup_funding": "تمويل شركات ناشئة",
    "project_funding": "تمويل مشروعات",
    "exchange_program": "برنامج تبادل",
    "conference": "مؤتمر",
    "youth_program": "برنامج شبابي",
    "volunteer": "تطوع",
    "course": "كورس",
    "certification": "شهادة",
    "other": "فرصة",
}
_MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر",
              "نوفمبر", "ديسمبر"]


def format_date_ar(date: dt.date) -> str:
    return f"{date.day} {_MONTHS_AR[date.month - 1]} {date.year}"


@dataclasses.dataclass
class PostFacts:
    """The only facts a post may contain."""

    extraction: ExtractedOpportunity
    verification_level: str | None = None
    warnings: list[str] = dataclasses.field(default_factory=list)

    @property
    def urls(self) -> set[str]:
        return {k for k in (normalize_url(self.extraction.application_url), normalize_url(self.extraction.official_url)) if k}

    @property
    def dates(self) -> set[dt.date]:
        out = set()
        for value in (self.extraction.deadline, self.extraction.start_date, self.extraction.end_date):
            if value:
                out.add(dt.date.fromisoformat(value))
        return out

    def fact_text(self) -> str:
        """All fact values as one string (used to whitelist numbers)."""
        data = self.extraction.model_dump()
        parts = [str(v) for k, v in data.items() if v not in (None, "", [], False) and k not in ("confidence",)]
        parts += [str(d.day) for d in self.dates] + [str(d.year) for d in self.dates]
        return " ".join(parts)

    def as_prompt_json(self) -> dict[str, Any]:
        data = {k: v for k, v in self.extraction.model_dump().items()
                if k not in ("confidence", "missing_fields", "suspicious_signals", "is_opportunity",
                             "not_opportunity_reason", "language")}
        if self.extraction.deadline:
            data["deadline_arabic"] = format_date_ar(dt.date.fromisoformat(self.extraction.deadline))
        return data


# --------------------------------------------------------------------------
# Fact guard (deterministic)
# --------------------------------------------------------------------------

_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")


@dataclasses.dataclass
class GuardResult:
    ok: bool
    violations: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "violations": self.violations}


def fact_guard(post_text: str, facts: PostFacts) -> GuardResult:
    violations: list[str] = []
    allowed_urls = facts.urls
    for link in extract_links(post_text):
        key = normalize_url(link)
        if key not in allowed_urls:
            violations.append(f"link not in the verified facts: {link}")
    allowed_dates = facts.dates
    for date in find_dates(post_text):
        if date not in allowed_dates:
            violations.append(f"date not in the verified facts: {date.isoformat()}")
    fact_numbers = {n.replace(",", "") for n in _NUMBER.findall(normalize_text(facts.fact_text()))}
    text_without_links = post_text
    for link in extract_links(post_text):
        text_without_links = text_without_links.replace(link, " ")
    date_digits = {str(d.day) for d in allowed_dates} | {str(d.year) for d in allowed_dates} | {
        f"{d.month}" for d in allowed_dates} | {f"{d.month:02d}" for d in allowed_dates} | {f"{d.day:02d}" for d in allowed_dates}
    for number in _NUMBER.findall(normalize_text(text_without_links)):
        plain = number.replace(",", "")
        if plain not in fact_numbers and plain not in date_digits:
            violations.append(f"number not in the verified facts: {number}")
    if facts.extraction.application_url or facts.extraction.official_url:
        if not any(normalize_url(link) in allowed_urls for link in extract_links(post_text)):
            violations.append("the official/application link is missing from the post")
    if facts.extraction.deadline:
        deadline = dt.date.fromisoformat(facts.extraction.deadline)
        if deadline not in set(find_dates(post_text)):
            violations.append("the deadline is missing from the post")
    return GuardResult(ok=not violations, violations=list(dict.fromkeys(violations)))


# --------------------------------------------------------------------------
# Template writer
# --------------------------------------------------------------------------

def _line(icon: str, label: str, value: str | None, *, required: bool = False) -> str | None:
    if value:
        return f"{icon} {label}: {value}"
    if required:
        return f"{icon} {label}: {NOT_STATED_AR}"
    return None


def render_template(facts: PostFacts) -> str:
    e = facts.extraction
    type_label = TYPE_LABELS_AR.get(e.opportunity_type or "other", "فرصة")
    lines: list[str | None] = [f"🎓 {type_label}: *{e.title}*", ""]
    lines.append(_line("🏛️", "الجهة", e.organization, required=True))
    if e.description:
        lines.append(f"📝 {e.description}")
    lines.append(_line("✅", "الشروط", e.eligibility, required=True))
    if e.age_min is not None or e.age_max is not None:
        if e.age_min is not None and e.age_max is not None:
            ages = f"من {e.age_min} إلى {e.age_max} سنة"
        elif e.age_min is not None:
            ages = f"من {e.age_min} سنة فأكثر"
        else:
            ages = f"حتى {e.age_max} سنة"
        lines.append(f"🎂 السن: {ages}")
    lines.append(_line("🌍", "الجنسيات/الإقامة", e.country_requirement))
    location = "أونلاين" if e.is_online else e.location
    lines.append(_line("📍", "المكان", location))
    lines.append(_line("💰", "التمويل", e.funding, required=True))
    if e.start_date:
        lines.append(f"📅 البداية: {format_date_ar(dt.date.fromisoformat(e.start_date))}")
    if e.required_documents:
        lines.append("📄 المطلوب للتقديم:")
        lines.extend(f"   • {doc}" for doc in e.required_documents[:10])
    deadline = format_date_ar(dt.date.fromisoformat(e.deadline)) if e.deadline else None
    lines.append(_line("⏰", "آخر موعد للتقديم", deadline, required=True))
    lines.append("")
    if e.application_url:
        lines.append(f"🔗 التقديم من هنا: {e.application_url}")
    if e.official_url and e.official_url != e.application_url:
        lines.append(f"🌐 الموقع الرسمي: {e.official_url}")
    if not e.application_url and not e.official_url:
        lines.append(f"🔗 رابط التقديم: {NOT_STATED_AR}")
    if facts.warnings:
        lines.append("")
        lines.append("⚠️ ملاحظات: " + " | ".join(facts.warnings[:3]))
    lines.append("")
    lines.append("📌 تأكد دايمًا من التفاصيل من الموقع الرسمي قبل التقديم.")
    lines.append(APEX_SIGNATURE)
    return "\n".join(line for line in lines if line is not None).strip()


# --------------------------------------------------------------------------
# LLM writer
# --------------------------------------------------------------------------

class WriterOutput(BaseModel):
    post_text: str = Field(min_length=40, max_length=3500)


WRITER_SYSTEM_PROMPT = f"""\
You write WhatsApp posts for APEX, a community that shares legitimate \
opportunities with Arab and Egyptian students.

Style: clear Arabic (natural Egyptian phrasing is fine), professional, \
short, WhatsApp-friendly (*bold* for the title, one idea per line), a few \
emojis at most.

ABSOLUTE RULES:
1. Use ONLY the facts in FACTS. Do not add any fact, number, date, link, \
benefit or requirement that is not there.
2. Missing facts: write "{NOT_STATED_AR}" for organization, eligibility, \
funding and deadline when they are null; omit other missing fields.
3. Include the application/official link exactly as given and the deadline.
4. STYLE EXAMPLES are past posts about OTHER opportunities. Imitate their \
structure and tone only. Never copy their facts, links, dates or numbers.
5. End with: {APEX_SIGNATURE}
Return JSON: {{"post_text": "..."}}
"""


def build_writer_prompt(facts: PostFacts, examples: list[str], revision_notes: list[str] | None = None) -> str:
    import json

    parts = ["FACTS (the only allowed content):", json.dumps(facts.as_prompt_json(), ensure_ascii=False, indent=1)]
    if examples:
        parts.append("\nSTYLE EXAMPLES (style only -- their facts are about other opportunities):")
        for i, example in enumerate(examples, 1):
            parts.append(f"--- example {i} ---\n{example[:1500]}")
    if revision_notes:
        parts.append("\nFix these problems from the previous version:\n" + "\n".join(f"- {n}" for n in revision_notes))
    return "\n".join(parts)


@dataclasses.dataclass
class WrittenPost:
    body: str
    writer_mode: str  # template | llm | template_fallback
    guard: GuardResult
    llm_attempts: int = 0


async def write_post(
    facts: PostFacts,
    *,
    mode: str,
    llm: LLMClient | None,
    examples: list[str] | None = None,
    revision_notes: list[str] | None = None,
    max_guard_rounds: int = 2,
) -> WrittenPost:
    template_body = render_template(facts)
    if mode != "llm" or llm is None or not llm.available:
        return WrittenPost(template_body, "template", fact_guard(template_body, facts))

    notes = list(revision_notes or [])
    attempts = 0
    for _ in range(max(1, max_guard_rounds)):
        attempts += 1
        result = await llm.structured(
            system=WRITER_SYSTEM_PROMPT,
            user=build_writer_prompt(facts, examples or [], notes),
            schema=WriterOutput,
            stage="writer",
            max_output_tokens=1500,
        )
        body = result.value.post_text.strip()
        guard = fact_guard(body, facts)
        if guard.ok:
            return WrittenPost(body, "llm", guard, attempts)
        log_stage("writer_fact_guard_failed", violations=guard.violations[:3], attempt=attempts)
        notes = guard.violations
    # The model could not stay within the facts: use the safe template.
    return WrittenPost(template_body, "template_fallback", fact_guard(template_body, facts), attempts)
