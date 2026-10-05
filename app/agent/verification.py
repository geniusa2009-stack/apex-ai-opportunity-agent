"""Official-source verification.

Verification is evidence-based and mostly deterministic. It never asks an
LLM "is this true?":

1. Deterministic red flags from the facts themselves -- expired deadline,
   fee/payment requests, "guaranteed acceptance", shortened or invalid
   application links, a page that redirects to another site.
2. The official/application page is fetched (plain HTTP GET; size, time and
   redirect limits; private/loopback hosts refused) and the extracted facts
   are matched against its real text: title, organization, deadline, and
   "applications closed" notices. A deadline that the page states
   differently is a contradiction.
3. Every claim and every piece of evidence is written through the EXISTING
   D6 Claim/Evidence system (`app/services/evidence_verification.py`), which
   also produces its deterministic decision.
4. The agent summarises all of this in a `VerificationReport` with a
   `VerificationLevel`. Source trust only decides whether "source-only"
   evidence is a blocker or a warning -- it never skips a check.

Limitations are explicit, not hidden: pages that need JavaScript cannot be
read; the page fetch is one GET; `OFFICIAL` authority is never assigned
automatically (same rule as D6).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import ipaddress
import re
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.dates import find_dates
from app.agent.extraction import ExtractedOpportunity
from app.agent.normalization import (
    is_url_shortener,
    is_valid_http_url,
    normalize_organization,
    normalize_text,
    registrable_domain,
    title_tokens,
)
from app.agent.observability import log_stage
from app.agent.reputation import requires_official_confirmation
from app.models.agent_enums import VerificationLevel
from app.models.enums import VerificationDecisionStatus
from app.services import evidence_verification as d6

USER_AGENT = "APEXOpportunityVerifier/1.0 (+https://github.com/geniusa2009-stack/apex-backend; verification of shared links)"

# --------------------------------------------------------------------------
# Page fetching
# --------------------------------------------------------------------------


@dataclasses.dataclass
class FetchedPage:
    url: str
    final_url: str | None = None
    status_code: int | None = None
    title: str | None = None
    text: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None and 200 <= self.status_code < 300


class PageFetcher(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.parts.append(data)


def html_to_text(html: str) -> tuple[str | None, str]:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML must not crash verification
        pass
    title = " ".join(" ".join(parser.title_parts).split()) or None
    text = " ".join(" ".join(parser.parts).split())
    return title, text


def _is_public_host(host: str) -> bool:
    """Refuse loopback/private/link-local literals and local names (basic SSRF guard).
    DNS names are not resolved here; the HTTP client runs with no access to
    internal credentials either way."""
    host = host.strip("[]").lower()
    if host in {"localhost"} or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host
    return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
                or address.is_multicast or address.is_unspecified)


class HttpPageFetcher:
    def __init__(self, *, timeout_seconds: float = 10.0, max_bytes: int = 1_500_000, max_redirects: int = 5,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._transport = transport

    async def fetch(self, url: str) -> FetchedPage:
        page = FetchedPage(url=url)
        host = urlsplit(url).hostname or ""
        if not is_valid_http_url(url) or not _is_public_host(host):
            page.error = "refused: not a public http(s) URL"
            return page
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                max_redirects=self._max_redirects,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"},
                transport=self._transport,
            ) as client:
                async with client.stream("GET", url) as response:
                    page.status_code = response.status_code
                    page.final_url = str(response.url)
                    if not _is_public_host(response.url.host or ""):
                        page.error = "refused: redirected to a non-public host"
                        return page
                    content_type = response.headers.get("content-type", "")
                    if response.status_code >= 400:
                        page.error = f"HTTP {response.status_code}"
                        return page
                    if "html" not in content_type and "text/plain" not in content_type:
                        page.error = f"unsupported content type {content_type.split(';')[0] or 'unknown'}"
                        return page
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self._max_bytes:
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        except (httpx.TimeoutException, asyncio.TimeoutError):
            page.error = "timeout"
            return page
        except httpx.TooManyRedirects:
            page.error = "too many redirects"
            return page
        except httpx.HTTPError as exc:
            page.error = f"network error: {type(exc).__name__}"
            return page
        if "html" in (content_type or ""):
            page.title, page.text = html_to_text(raw)
        else:
            page.text = " ".join(raw.split())
        return page


class NullPageFetcher:
    """Used when page fetching is disabled: verification degrades to
    source-only evidence, explicitly."""

    async def fetch(self, url: str) -> FetchedPage:
        return FetchedPage(url=url, error="page fetching disabled")


# --------------------------------------------------------------------------
# Deterministic checks
# --------------------------------------------------------------------------

_PAYMENT_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"فودافون\s*كاش|vodafone\s*cash|اورنج\s*كاش|instapay|انستا\s*باي|etisalat\s*cash|we\s*pay",
        r"(رسوم|مصاريف)\s*(التسجيل|التقديم|الاشتراك|حجز)",
        r"(registration|application|processing)\s+fee",
        r"(ادفع|حول|تحويل)\s+\d+",
    )
]
_GUARANTEE_PATTERNS = [
    re.compile(p, re.I)
    for p in (r"قبول\s*مضمون|مضمون\s*100|100\s*%\s*(قبول|acceptance)|guaranteed\s+(acceptance|admission|visa)",)
]
_CLOSED_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"applications?\s+(are\s+)?(now\s+)?closed",
        r"(call|registration)\s+(is\s+)?closed",
        r"no\s+longer\s+accepting\s+applications",
        r"(التقديم|التسجيل)\s+(مغلق|انتهى|اغلق)",
        r"(انتهى|اغلق)\s+(التقديم|التسجيل)",
    )
]
_DEADLINE_KEYWORDS = re.compile(
    r"(deadline|closing date|apply by|applications? close|due date|اخر موعد|الموعد النهائي|موعد اغلاق|حتى تاريخ)",
    re.I,
)
_STOPWORDS = frozenset({"the", "and", "for", "of", "in", "to", "a", "an", "program", "programme", "في", "من", "الى",
                        "على", "عن", "مع", "او", "و", "ل", "لل"})


def deadline_dates_on_page(text: str) -> list[dt.date]:
    """Dates that appear within 120 characters after a deadline keyword."""
    normalized = normalize_text(text)
    dates: list[dt.date] = []
    for match in _DEADLINE_KEYWORDS.finditer(normalized):
        window = normalized[match.end(): match.end() + 120]
        for date in find_dates(window):
            if date not in dates:
                dates.append(date)
    return dates


@dataclasses.dataclass
class CheckResult:
    name: str
    status: str  # "pass" | "fail" | "warn" | "skip"
    detail: str

    def as_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class VerificationReport:
    level: VerificationLevel
    checks: list[CheckResult]
    red_flags: list[str]
    d6_decision: str | None
    page_url: str | None
    page_final_url: str | None
    blocking: bool
    expired: bool
    confirmed_claims: list[str]
    contradicted_claims: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "checks": [c.as_dict() for c in self.checks],
            "red_flags": self.red_flags,
            "d6_decision": self.d6_decision,
            "page_url": self.page_url,
            "page_final_url": self.page_final_url,
            "blocking": self.blocking,
            "expired": self.expired,
            "confirmed_claims": self.confirmed_claims,
            "contradicted_claims": self.contradicted_claims,
        }


def message_red_flags(extraction: ExtractedOpportunity, text: str) -> list[str]:
    flags: list[str] = []
    normalized = normalize_text(text)
    if extraction.requires_payment:
        flags.append("applicants are asked to pay a fee")
    if any(p.search(normalized) for p in _PAYMENT_PATTERNS):
        flags.append("payment request pattern in the message (fees / mobile wallet transfer)")
    if any(p.search(normalized) for p in _GUARANTEE_PATTERNS):
        flags.append("promises guaranteed acceptance")
    for url in (extraction.application_url, extraction.official_url):
        if url and is_url_shortener(url):
            flags.append(f"shortened link hides the real site: {url}")
    if not extraction.application_url and not extraction.official_url:
        flags.append("no application or official link")
    for signal in extraction.suspicious_signals:
        flags.append(f"extractor noted: {signal}")
    return list(dict.fromkeys(flags))


def _page_contains_tokens(page_text_norm: str, tokens: frozenset[str]) -> float:
    meaningful = [t for t in tokens if len(t) > 2 and t not in _STOPWORDS]
    if not meaningful:
        return 0.0
    hits = sum(1 for t in meaningful if t in page_text_norm)
    return hits / len(meaningful)


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------


async def verify_opportunity(
    db: AsyncSession,
    *,
    opportunity_id,
    extraction: ExtractedOpportunity,
    message_text: str,
    source_id,
    source_type: str,
    trust_score: float,
    fetcher: PageFetcher,
    today: dt.date | None = None,
) -> VerificationReport:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    checks: list[CheckResult] = []
    confirmed: list[str] = []
    contradicted: list[str] = []
    red_flags = message_red_flags(extraction, message_text)

    # --- expiry --------------------------------------------------------------
    deadline = extraction.deadline_date
    expired = bool(deadline and deadline < today)
    if deadline is None:
        checks.append(CheckResult("deadline_known", "warn", "no deadline stated in the source"))
    elif expired:
        checks.append(CheckResult("deadline_not_passed", "fail", f"deadline {deadline} is before {today}"))
    else:
        checks.append(CheckResult("deadline_not_passed", "pass", f"deadline {deadline}"))

    # --- D6 claims + source-message evidence --------------------------------
    claim_values = {
        "title": extraction.title,
        "organization": extraction.organization,
        "opportunity_type": extraction.opportunity_type,
        "deadline": extraction.deadline,
        "eligibility": extraction.eligibility,
        "funding": extraction.funding,
        "application_url": extraction.application_url,
    }
    claims = {}
    for claim_type, value in claim_values.items():
        if not value:
            continue
        claim = await d6.get_or_create_claim(db, opportunity_id=opportunity_id, claim_type=claim_type,
                                             claim_value=str(value)[:2000])
        claims[claim_type] = claim
        await d6.record_evidence(
            db,
            opportunity_id=opportunity_id,
            claim_id=claim.id,
            evidence_text=message_text[:2000],
            source_id=source_id,
            source_type=source_type,
            extraction_method="llm_extraction",
            retrieval_method=f"{source_type}_message",
            supports_claim=True,
        )

    # --- official page --------------------------------------------------------
    page_url = extraction.application_url or extraction.official_url
    page: FetchedPage | None = None
    if page_url and is_valid_http_url(page_url) and not is_url_shortener(page_url):
        page = await fetcher.fetch(page_url)
        if page.ok:
            checks.append(CheckResult("official_page_reachable", "pass", f"HTTP {page.status_code}"))
            original_site = registrable_domain(page_url)
            final_site = registrable_domain(page.final_url or page_url)
            if original_site and final_site and original_site != final_site:
                red_flags.append(f"link redirects from {original_site} to {final_site}")
                checks.append(CheckResult("same_site_after_redirect", "warn", f"{original_site} -> {final_site}"))
            page_norm = normalize_text(f"{page.title or ''} {page.text}")

            def evidence(claim_type: str, supports: bool, snippet: str) -> None:
                if claim_type in claims:
                    pending.append((claim_type, supports, snippet))

            pending: list[tuple[str, bool, str]] = []
            title_score = _page_contains_tokens(page_norm, title_tokens(extraction.title))
            if title_score >= 0.6:
                confirmed.append("title")
                evidence("title", True, f"page mentions {title_score:.0%} of the title keywords")
                checks.append(CheckResult("title_on_page", "pass", f"{title_score:.0%} of title keywords"))
            else:
                checks.append(CheckResult("title_on_page", "warn", f"only {title_score:.0%} of title keywords"))

            if extraction.organization:
                org = normalize_organization(extraction.organization)
                org_tokens = frozenset(t for t in org.split(" ") if t)
                org_score = _page_contains_tokens(page_norm, org_tokens)
                site = (final_site or "").split(".")[0]
                if org_score >= 0.6 or (site and site in org.replace(" ", "")):
                    confirmed.append("organization")
                    evidence("organization", True, "organization named on the page / matches the site")
                    checks.append(CheckResult("organization_on_page", "pass", extraction.organization))
                else:
                    checks.append(CheckResult("organization_on_page", "warn", "organization not found on the page"))

            if deadline:
                page_dates = find_dates(page.text)
                page_deadlines = deadline_dates_on_page(page.text)
                if deadline in page_dates:
                    confirmed.append("deadline")
                    evidence("deadline", True, f"page states {deadline.isoformat()}")
                    checks.append(CheckResult("deadline_on_page", "pass", deadline.isoformat()))
                elif page_deadlines:
                    contradicted.append("deadline")
                    evidence("deadline", False, f"page deadline {[d.isoformat() for d in page_deadlines]}")
                    checks.append(CheckResult("deadline_on_page", "fail",
                                              f"page says {', '.join(d.isoformat() for d in page_deadlines)}"))
                else:
                    checks.append(CheckResult("deadline_on_page", "warn", "deadline not found on the page"))

            if any(p.search(page_norm) for p in _CLOSED_PATTERNS):
                contradicted.append("open_for_applications")
                checks.append(CheckResult("applications_open", "fail", "page says applications are closed"))

            if "application_url" in claims:
                pending.append(("application_url", True, f"page reachable at {page.final_url or page_url}"))

            for claim_type, supports, snippet in pending:
                try:
                    await d6.record_evidence(
                        db,
                        opportunity_id=opportunity_id,
                        claim_id=claims[claim_type].id,
                        evidence_text=snippet,
                        source_url=page.final_url or page_url,
                        source_type="official_page",
                        extraction_method="deterministic_match",
                        retrieval_method="http_get",
                        supports_claim=supports,
                    )
                except d6.EvidenceRejectedError as exc:  # never fabricate a fallback
                    checks.append(CheckResult("evidence_recorded", "warn", str(exc)[:200]))
        else:
            reason = page.error or f"HTTP {page.status_code}"
            if page.status_code in (404, 410):
                contradicted.append("application_url")
                if "application_url" in claims:
                    await d6.record_evidence(
                        db, opportunity_id=opportunity_id, claim_id=claims["application_url"].id,
                        evidence_text=f"link returned HTTP {page.status_code}", source_url=page_url,
                        source_type="official_page", extraction_method="deterministic_match",
                        retrieval_method="http_get", supports_claim=False,
                    )
                checks.append(CheckResult("official_page_reachable", "fail", reason))
            else:
                checks.append(CheckResult("official_page_reachable", "warn", f"could not verify: {reason}"))
    elif page_url and is_url_shortener(page_url):
        checks.append(CheckResult("official_page_reachable", "warn", "shortened link not followed"))
    else:
        checks.append(CheckResult("official_page_reachable", "skip", "no application/official link"))

    decision = await d6.decide_verification(db, opportunity_id=opportunity_id, claims=list(claims.values()))

    # --- summary level -------------------------------------------------------
    if expired:
        level = VerificationLevel.REJECTED
    elif contradicted or decision.status in (VerificationDecisionStatus.HOLD, VerificationDecisionStatus.REJECTED):
        level = VerificationLevel.CONTRADICTED
    elif page is not None and page.ok and "title" in confirmed and (
        not extraction.deadline or "deadline" in confirmed
    ) and (not extraction.organization or "organization" in confirmed):
        level = VerificationLevel.OFFICIAL_CONFIRMED
    elif confirmed:
        level = VerificationLevel.PARTIALLY_CONFIRMED
    else:
        level = VerificationLevel.SOURCE_ONLY

    serious_flags = [f for f in red_flags if f.startswith(("applicants are asked to pay", "payment request",
                                                            "promises guaranteed"))]
    blocking = (
        level in (VerificationLevel.CONTRADICTED, VerificationLevel.REJECTED)
        or bool(serious_flags)
        or (level == VerificationLevel.SOURCE_ONLY and requires_official_confirmation(trust_score)
            and not extraction.application_url and not extraction.official_url)
    )
    report = VerificationReport(
        level=level,
        checks=checks,
        red_flags=red_flags,
        d6_decision=decision.status.value,
        page_url=page_url,
        page_final_url=page.final_url if page else None,
        blocking=blocking,
        expired=expired,
        confirmed_claims=confirmed,
        contradicted_claims=contradicted,
    )
    log_stage(
        "verification_done",
        opportunity_id=opportunity_id,
        verification_level=level.value,
        d6=decision.status.value,
        blocking=blocking,
        confirmed=confirmed,
        contradicted=contradicted,
        red_flags=len(red_flags),
    )
    return report
