"""Structured logging for the AI Opportunity Agent.

Every pipeline log line is a single JSON object on the ``apex.agent``
logger with a fixed vocabulary: ``event``, ``job_id``, ``correlation_id``,
``source_id``, ``source_message_id``, ``opportunity_id``, ``stage``,
``status``, ``duration_ms``, ``error_code`` (+ small extras). That is enough
to answer "what message entered, which job processed it, what was
extracted/verified/decided, was it approved, was it published" with one
grep on the correlation id -- and the same answers are also persisted in
`pipeline_job_events`.

Secrets and personal data: values are passed through `redact()`, which
masks anything that looks like an API key/token/bearer header/secret and
truncates long free text. Phone numbers (WhatsApp sender ids) are masked to
their last 4 digits. Raw message text is never logged here.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from typing import Any

agent_logger = logging.getLogger("apex.agent")

_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(bearer)\s+[a-z0-9._\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),  # OpenAI/OpenRouter style keys
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),  # Google API keys
    re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{30,}\b"),  # Telegram bot tokens
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|signature)\s*[=:]\s*[^\s,;&]+"),
]
# "+20 100 123 4567" / "201001234567" -- but not dates ("2026-10-15") or ids.
_PHONE = re.compile(r"(?<![\w/-])\+\d[\d \-]{8,16}\d|(?<![\w/.-])\d{10,15}(?![\w.-])")
_ID_FIELDS = frozenset(
    {"job_id", "correlation_id", "source_id", "source_message_id", "opportunity_id", "draft_id", "publication_id"}
)
_MAX_VALUE_LEN = 300


def mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"


def redact(value: Any) -> Any:
    """Mask secret-looking substrings and phone numbers; truncate long text."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {k: ("***" if re.search(r"(?i)key|token|secret|password|authorization", str(k)) else redact(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value][:20]
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + " ***") if m.lastindex else "***", text)
    text = _PHONE.sub(lambda m: mask_phone(m.group(0)), text)
    if len(text) > _MAX_VALUE_LEN:
        text = text[:_MAX_VALUE_LEN] + "…"
    return text


def log_stage(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured, redacted log record."""
    record: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if value is None:
            continue
        if key in _ID_FIELDS:
            record[key] = str(value)
        else:
            record[key] = redact(value)
    agent_logger.log(level, json.dumps(record, ensure_ascii=False, default=str))


@contextmanager
def timed():
    """``with timed() as t: ...; t()`` -> elapsed milliseconds."""
    start = time.perf_counter()
    elapsed = {"ms": 0}

    def get() -> int:
        return int((time.perf_counter() - start) * 1000)

    try:
        yield get
    finally:
        elapsed["ms"] = get()
