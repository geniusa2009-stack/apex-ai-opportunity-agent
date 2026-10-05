"""Reviewer + Quality Gate.

The deterministic quality gate ALWAYS runs and cannot be overridden by an
LLM. An optional LLM review (AGENT_REVIEWER_LLM_ENABLED) may only make the
verdict stricter -- it can add issues, never remove the gate's.

Output: APPROVE / NEEDS_REVISION / REJECT with structured reasons. APPROVE
here means "good enough to put in front of a human", never "publish".
"""

from __future__ import annotations

import dataclasses
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.llm import LLMClient
from app.agent.writer import APEX_SIGNATURE, PostFacts, fact_guard
from app.models.agent_enums import DedupOutcome, ReviewDecision, VerificationLevel

Severity = Literal["error", "warning", "info"]
_RANK = {ReviewDecision.APPROVE: 0, ReviewDecision.NEEDS_REVISION: 1, ReviewDecision.REJECT: 2}


@dataclasses.dataclass
class ReviewIssue:
    code: str
    severity: Severity
    message: str
    fixable_by_rewrite: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ReviewResult:
    decision: ReviewDecision
    issues: list[ReviewIssue]
    llm_used: bool = False

    @property
    def revision_notes(self) -> list[str]:
        return [i.message for i in self.issues if i.fixable_by_rewrite and i.severity == "error"]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [i.as_dict() for i in self.issues]


def quality_gate(
    post_text: str,
    facts: PostFacts,
    *,
    verification_level: str | None,
    red_flags: list[str] | None = None,
    dedup_outcome: str | None = None,
    duplicate_of: str | None = None,
) -> ReviewResult:
    e = facts.extraction
    issues: list[ReviewIssue] = []

    def add(code: str, severity: Severity, message: str, fixable: bool = False) -> None:
        issues.append(ReviewIssue(code, severity, message, fixable))

    # --- required information --------------------------------------------
    if not e.is_opportunity:
        add("NOT_AN_OPPORTUNITY", "error", "the source is not an opportunity")
    if not e.title:
        add("MISSING_TITLE", "error", "no title")
    if not e.application_url and not e.official_url:
        add("MISSING_LINK", "warning", "no application or official link -- readers cannot apply")
    if not e.deadline:
        add("MISSING_DEADLINE", "warning", "deadline not stated in the source")
    if not e.organization:
        add("MISSING_ORGANIZATION", "warning", "organization not stated")
    if not e.eligibility:
        add("UNCLEAR_ELIGIBILITY", "warning", "eligibility not stated -- readers cannot tell if they qualify")

    # --- verification / trust ----------------------------------------------
    level = verification_level or ""
    if level == VerificationLevel.REJECTED.value:
        add("VERIFICATION_REJECTED", "error", "verification rejected the opportunity (e.g. expired)")
    elif level == VerificationLevel.CONTRADICTED.value:
        add("CONTRADICTED", "error", "the official source contradicts the facts")
    elif level == "human_override":
        add("VERIFICATION_OVERRIDDEN", "warning",
            "a team member let this continue despite verification problems -- re-check before approving")
    elif level == VerificationLevel.SOURCE_ONLY.value:
        add("SOURCE_ONLY", "warning", "facts are supported only by the incoming message, not by an official page")
    elif level == VerificationLevel.PARTIALLY_CONFIRMED.value:
        add("PARTIALLY_CONFIRMED", "info", "the official page confirmed some, not all, facts")
    for flag in red_flags or []:
        add("RED_FLAG", "warning", flag)

    # --- duplicate risk -------------------------------------------------------
    if dedup_outcome == DedupOutcome.POSSIBLE_DUPLICATE.value:
        add("POSSIBLE_DUPLICATE", "warning", f"possible duplicate of opportunity {duplicate_of}")

    # --- facts in the text ---------------------------------------------------
    guard = fact_guard(post_text, facts)
    for violation in guard.violations:
        add("UNSUPPORTED_CLAIM", "error", violation, fixable=True)

    # --- formatting -------------------------------------------------------------
    stripped = post_text.strip()
    if len(stripped) < 80:
        add("TOO_SHORT", "error", "post is too short", fixable=True)
    if len(stripped) > 3000:
        add("TOO_LONG", "error", "post is longer than 3000 characters", fixable=True)
    if stripped.count("*") % 2:
        add("BROKEN_FORMATTING", "error", "unbalanced *bold* markers", fixable=True)
    if APEX_SIGNATURE not in stripped:
        add("MISSING_SIGNATURE", "warning", "APEX signature missing", fixable=True)
    if e.title and e.title.split()[0] not in stripped.split("\n")[0] and e.title not in stripped[:300]:
        add("TITLE_NOT_PROMINENT", "warning", "title is not at the top of the post", fixable=True)

    errors = [i for i in issues if i.severity == "error"]
    if any(i.code in {"NOT_AN_OPPORTUNITY", "VERIFICATION_REJECTED", "CONTRADICTED"} for i in errors):
        decision = ReviewDecision.REJECT
    elif errors:
        decision = ReviewDecision.NEEDS_REVISION
    else:
        decision = ReviewDecision.APPROVE
    return ReviewResult(decision, issues)


# --------------------------------------------------------------------------
# Optional LLM review (one call)
# --------------------------------------------------------------------------

class LLMReviewIssue(BaseModel):
    code: Literal["UNSUPPORTED_CLAIM", "MISLEADING", "UNCLEAR", "FORMATTING", "INCOMPLETE", "OTHER"]
    message: str = Field(max_length=300)


class LLMReview(BaseModel):
    decision: Literal["approve", "needs_revision", "reject"]
    issues: list[LLMReviewIssue] = Field(default_factory=list, max_length=10)


REVIEW_SYSTEM_PROMPT = """\
You are the APEX quality reviewer. Compare POST with FACTS.
Flag: any claim in POST not supported by FACTS, misleading wording (e.g. \
"guaranteed", "fully funded" when FACTS do not say so), missing key \
information, unclear eligibility, broken formatting.
decision: approve (accurate and clear), needs_revision (fixable wording \
problems), reject (misleading or not a real opportunity).
Return JSON: {"decision": "...", "issues": [{"code": "...", "message": "..."}]}
"""


async def review_post(
    post_text: str,
    facts: PostFacts,
    *,
    verification_level: str | None,
    red_flags: list[str] | None = None,
    dedup_outcome: str | None = None,
    duplicate_of: str | None = None,
    llm: LLMClient | None = None,
    use_llm: bool = False,
) -> ReviewResult:
    result = quality_gate(
        post_text,
        facts,
        verification_level=verification_level,
        red_flags=red_flags,
        dedup_outcome=dedup_outcome,
        duplicate_of=duplicate_of,
    )
    # Only spend an LLM call when the deterministic gate passed.
    if not use_llm or llm is None or not llm.available or result.decision != ReviewDecision.APPROVE:
        return result
    import json

    llm_result = await llm.structured(
        system=REVIEW_SYSTEM_PROMPT,
        user=f"FACTS:\n{json.dumps(facts.as_prompt_json(), ensure_ascii=False)}\n\nPOST:\n{post_text}",
        schema=LLMReview,
        stage="review",
        max_output_tokens=800,
    )
    llm_decision = ReviewDecision(llm_result.value.decision)
    for issue in llm_result.value.issues:
        severity: Severity = "error" if llm_decision != ReviewDecision.APPROVE else "info"
        result.issues.append(ReviewIssue(f"LLM_{issue.code}", severity, issue.message,
                                         fixable_by_rewrite=llm_decision == ReviewDecision.NEEDS_REVISION))
    if _RANK[llm_decision] > _RANK[result.decision]:
        result.decision = llm_decision
    result.llm_used = True
    return result
