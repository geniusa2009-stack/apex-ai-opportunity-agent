"""Deterministic normalization used before any deduplication or AI call.

Everything here is pure, side-effect free, and heavily unit-tested
(tests/agent/test_normalization.py). No network access: a shortened URL is
*detected*, never resolved here (resolution would be a network call and is
the verifier's job).

Design notes
------------
* `normalize_url` returns a **comparison key**, not a guaranteed-fetchable
  URL: scheme is folded to https, ``www.`` is dropped, tracking parameters
  are removed and the remaining query parameters are sorted. Two links that
  normalize to the same key point at the same resource for dedup purposes.
* Arabic text gets the standard search normalizations (diacritics and
  tatweel removed, alef/yaa/taa-marbuta variants folded, Arabic-Indic digits
  to ASCII) so the same post written slightly differently hashes the same.
* `content_hash` deliberately ignores WhatsApp formatting markers, emojis
  and punctuation so a forwarded copy ("*Forwarded*", bold markers) of the
  same message is an exact content duplicate.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

_ZERO_WIDTH_AND_BIDI = re.compile("[​-‏‪-‮⁠-⁤﻿؜]")
_ARABIC_DIACRITICS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"
_ALEF_VARIANTS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})
_YAA_TAA = str.maketrans({"ى": "ي", "ة": "ه"})
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_WHITESPACE = re.compile(r"\s+")
_WHATSAPP_MARKUP = re.compile(r"[*_~`]+")


def normalize_text(text: str | None) -> str:
    """Unicode/Arabic/whitespace/case normalization that keeps punctuation.

    Used for display-independent comparisons and as the base of the other
    normalizers. Returns ``""`` for ``None``/blank input.
    """
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text)
    value = _ZERO_WIDTH_AND_BIDI.sub("", value)
    value = _ARABIC_DIACRITICS.sub("", value).replace(_TATWEEL, "")
    value = value.translate(_ALEF_VARIANTS).translate(_YAA_TAA).translate(_DIGITS)
    value = value.casefold()
    return _WHITESPACE.sub(" ", value).strip()


def _strip_symbols(value: str) -> str:
    """Keep only letters, digits and spaces (drops punctuation and emoji)."""
    out = []
    for ch in value:
        category = unicodedata.category(ch)
        if category[0] in ("L", "N"):
            out.append(ch)
        else:
            out.append(" ")
    return _WHITESPACE.sub(" ", "".join(out)).strip()


# Decorative prefixes that carry no identity information. Kept short and
# conservative on purpose: words like "منحة"/"scholarship" are identity
# information and are NOT removed. Years are kept (2026 vs 2025 editions
# are different opportunities / an update, not a duplicate).
_TITLE_NOISE_TOKENS: frozenset[str] = frozenset(
    {
        "عاجل",
        "هام",
        "مهم",
        "جديد",
        "جديده",
        "حصري",
        "urgent",
        "new",
        "hot",
        "important",
        "deadline",
        "extended",
        "fwd",
        "forwarded",
    }
)


def normalize_title(title: str | None) -> str:
    """Comparison key for titles: normalized text without punctuation/emoji
    and without purely decorative tokens."""
    base = _strip_symbols(_WHATSAPP_MARKUP.sub(" ", normalize_text(title)))
    tokens = [tok for tok in base.split(" ") if tok and tok not in _TITLE_NOISE_TOKENS]
    return " ".join(tokens)


def title_tokens(title: str | None) -> frozenset[str]:
    return frozenset(tok for tok in normalize_title(title).split(" ") if len(tok) > 1)


def title_similarity(a: str | None, b: str | None) -> float:
    """Deterministic token Jaccard similarity of two normalized titles, 0..1."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def normalize_organization(name: str | None) -> str:
    """Comparison key for organization names (drops legal suffixes)."""
    base = _strip_symbols(normalize_text(name))
    suffixes = {"inc", "ltd", "llc", "foundation", "org", "the", "co", "company", "مؤسسه", "شركه"}
    return " ".join(tok for tok in base.split(" ") if tok and tok not in suffixes)


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

# Exact names, compared case-insensitively. Anything starting with "utm_"
# is also dropped. The list is deliberately limited to parameters that are
# documented tracking/share parameters; functional parameters are kept.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "twclid",
        "ttclid",
        "li_fat_id",
        "igshid",
        "igsh",
        "mibextid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "ref_src",
        "si",
    }
)

URL_SHORTENER_HOSTS: frozenset[str] = frozenset(
    {
        "bit.ly",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "ow.ly",
        "cutt.ly",
        "rb.gy",
        "shorturl.at",
        "is.gd",
        "buff.ly",
        "lnkd.in",
        "rebrand.ly",
        "t.ly",
        "s.id",
        "shorte.st",
        "adf.ly",
    }
)

# Redirect wrappers whose real target is in a query parameter.
_REDIRECT_WRAPPERS: dict[str, str] = {
    "l.facebook.com": "u",
    "lm.facebook.com": "u",
    "l.instagram.com": "u",
    "google.com": "q",  # only for the /url path, checked below
    "l.wl.co": "u",
}

# Two-label public suffixes common in APEX's region. Not a full PSL -- a
# documented, conservative approximation used only for "same site?" checks.
_TWO_LABEL_SUFFIXES: frozenset[str] = frozenset(
    {
        "com.eg", "edu.eg", "gov.eg", "org.eg", "net.eg", "sci.eg", "eun.eg",
        "co.uk", "ac.uk", "org.uk", "gov.uk",
        "com.au", "edu.au", "gov.au",
        "com.sa", "edu.sa", "gov.sa",
        "ac.jp", "co.jp", "go.jp",
        "edu.tr", "gov.tr", "com.tr",
        "ac.ae", "gov.ae", "co.ae",
        "edu.cn", "gov.cn", "com.cn",
        "ac.in", "gov.in", "co.in",
        "co.za", "ac.za",
        "edu.pk", "gov.pk",
        "ac.kr", "go.kr",
    }
)

_URL_IN_TEXT = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'«»“”]+")
_TRAILING_URL_PUNCT = ".,;:!?)]}>'\"،؛؟…*_~"


def _clean_raw_url(url: str) -> str:
    value = url.strip().strip("<>\"'").lstrip("([{")
    while value and value[-1] in _TRAILING_URL_PUNCT:
        # Keep a closing paren that balances an opening one inside the URL
        # (e.g. Wikipedia-style links).
        if value[-1] == ")" and value.count("(") >= value.count(")"):
            break
        value = value[:-1]
    return value


def extract_links(text: str | None) -> list[str]:
    """Return the http(s)/www links found in `text`, cleaned, in order, de-duplicated."""
    if not text:
        return []
    seen: set[str] = set()
    links: list[str] = []
    for match in _URL_IN_TEXT.finditer(unicodedata.normalize("NFKC", text)):
        link = _clean_raw_url(match.group(0))
        if link.lower().startswith("www."):
            link = "https://" + link
        if link and link not in seen:
            seen.add(link)
            links.append(link)
    return links


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_url_shortener(url: str | None) -> bool:
    if not url:
        return False
    host = _host_of(url if "://" in url else f"https://{url}")
    host = host[4:] if host.startswith("www.") else host
    return host in URL_SHORTENER_HOSTS


def is_valid_http_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname
    # Require a dot in the host (no bare "localhost"/intranet names) and no spaces.
    return "." in host and " " not in url.strip()


def _unwrap_redirect(parts) -> str | None:
    host = (parts.hostname or "").lower()
    bare = host[4:] if host.startswith("www.") else host
    param = _REDIRECT_WRAPPERS.get(bare)
    if not param:
        return None
    if bare == "google.com" and parts.path != "/url":
        return None
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        if key == param and value.startswith(("http://", "https://")):
            return value
    return None


def normalize_url(url: str | None, *, _depth: int = 0) -> str | None:
    """Canonical comparison key for a URL, or ``None`` if it is not a valid
    http(s) URL.

    - trims surrounding punctuation, adds ``https://`` to ``www.`` links
    - unwraps known redirect wrappers (``l.facebook.com/l.php?u=``, ``google.com/url?q=``)
    - lowercases scheme/host, folds ``http`` to ``https``, drops ``www.``,
      default ports, fragments, duplicate/trailing slashes
    - removes tracking parameters, sorts the remaining query parameters
    """
    if not url:
        return None
    raw = _clean_raw_url(unicodedata.normalize("NFKC", url))
    if raw.lower().startswith("www."):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None

    if _depth < 2:
        target = _unwrap_redirect(parts)
        if target:
            unwrapped = normalize_url(unquote(target), _depth=_depth + 1)
            if unwrapped:
                return unwrapped

    host = parts.hostname.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if "." not in host:
        return None
    port = parts.port if parts.port not in (None, 80, 443) else None
    netloc = f"{host}:{port}" if port else host

    path = re.sub(r"/{2,}", "/", parts.path or "")
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    if path == "/":
        path = ""

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    ]
    kept.sort()
    query = urlencode(kept, doseq=True)
    return urlunsplit(("https", netloc, path, query, ""))


def registrable_domain(url: str | None) -> str | None:
    """Approximate registrable domain ("eun.eg" for "www.scholar.eun.eg").
    Used only to compare whether two URLs belong to the same site."""
    if not url:
        return None
    host = _host_of(url if "://" in url else f"https://{url}")
    if not host or "." not in host:
        return None
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# --------------------------------------------------------------------------
# Hashes
# --------------------------------------------------------------------------

def normalize_content(text: str | None) -> str:
    """Normalization used for the content hash: text normalization, URLs
    replaced by their normalized key, WhatsApp markup/punctuation/emoji
    removed, "forwarded" markers dropped."""
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text)
    for link in extract_links(value):
        key = normalize_url(link) or link
        value = value.replace(link, f" {key} ")
    value = normalize_text(value)
    value = _WHATSAPP_MARKUP.sub(" ", value)
    tokens: list[str] = []
    for token in value.split(" "):
        if token.startswith("https://"):
            tokens.append(token)
        else:
            cleaned = _strip_symbols(token)
            if cleaned:
                tokens.extend(cleaned.split(" "))
    tokens = [t for t in tokens if t not in {"forwarded", "fwd"}]
    return " ".join(tokens)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_hash(text: str | None) -> str | None:
    """Stable SHA-256 of `normalize_content(text)`; ``None`` for empty content."""
    normalized = normalize_content(text)
    return sha256_hex(normalized) if normalized else None
