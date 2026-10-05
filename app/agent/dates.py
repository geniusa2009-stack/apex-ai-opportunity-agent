"""Deterministic date parsing (English + Arabic, Egyptian and Levantine
month names, Arabic-Indic digits). No LLM involved.

* `parse_date(value)` -- parse ONE date string (e.g. what the extractor
  returned for `deadline`). Returns ``None`` when it is not a full,
  unambiguous calendar date (a bare "October" is not a deadline).
* `find_dates(text)` -- every full date mentioned in free text. Used by the
  pre-extraction dedup check and by the fact guard that verifies the
  Writer did not invent dates.

Numeric dates are read **day-first** (15/10/2026), the convention in Egypt
and the Arab world; ISO ``YYYY-MM-DD`` is always year-first.
"""

from __future__ import annotations

import datetime as dt
import re

from app.agent.normalization import normalize_text

_MONTHS: dict[str, int] = {
    # English
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    # Arabic (Egyptian / Gulf), already in normalize_text() form (أ/إ/آ -> ا, ى -> ي)
    "يناير": 1, "فبراير": 2, "مارس": 3, "ابريل": 4, "مايو": 5, "يونيو": 6, "يونيه": 6,
    "يوليو": 7, "يوليه": 7, "اغسطس": 8, "سبتمبر": 9, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
    # Levantine
    "كانون الثاني": 1, "شباط": 2, "اذار": 3, "نيسان": 4, "ايار": 5, "حزيران": 6, "تموز": 7,
    "اب": 8, "ايلول": 9, "تشرين الاول": 10, "تشرين الثاني": 11, "كانون الاول": 12,
}
# Longest names first so "كانون الثاني" wins over a shorter overlapping key.
_MONTH_ALT = "|".join(sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True))

_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_NUMERIC_DMY = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?!\d)")
_DAY_MONTH_YEAR = re.compile(rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({_MONTH_ALT})\.?,?\s*(\d{{4}})(?!\d)")
_MONTH_DAY_YEAR = re.compile(rf"(?<![\w])({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})(?!\d)")

_MIN_YEAR, _MAX_YEAR = 2000, 2100


def _make(year: int, month: int, day: int) -> dt.date | None:
    if not (_MIN_YEAR <= year <= _MAX_YEAR):
        return None
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def find_dates(text: str | None) -> list[dt.date]:
    """All full calendar dates in `text`, in order of appearance, unique."""
    if not text:
        return []
    value = normalize_text(text)
    found: list[tuple[int, dt.date]] = []
    for match in _ISO.finditer(value):
        date = _make(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if date:
            found.append((match.start(), date))
    for match in _NUMERIC_DMY.finditer(value):
        date = _make(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        if date:
            found.append((match.start(), date))
    for match in _DAY_MONTH_YEAR.finditer(value):
        date = _make(int(match.group(3)), _MONTHS[match.group(2)], int(match.group(1)))
        if date:
            found.append((match.start(), date))
    for match in _MONTH_DAY_YEAR.finditer(value):
        date = _make(int(match.group(3)), _MONTHS[match.group(1)], int(match.group(2)))
        if date:
            found.append((match.start(), date))
    found.sort(key=lambda item: item[0])
    unique: list[dt.date] = []
    for _, date in found:
        if date not in unique:
            unique.append(date)
    return unique


def parse_date(value: str | dt.date | dt.datetime | None) -> dt.date | None:
    """Parse a single date value. Accepts ISO datetimes too."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    dates = find_dates(text)
    return dates[0] if len(dates) == 1 else None
