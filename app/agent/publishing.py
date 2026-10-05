"""Publishing: a generic `PublisherConnector` interface + idempotent
`Publication` bookkeeping.

Idempotency key = sha256(opportunity_id | platform | target | draft_id).
The key is UNIQUE in the database, so retries, restarts, two workers or a
double-clicked "approve" all resolve to the same `Publication` row.

At-most-once delivery:

    PENDING/FAILED --(persist PUBLISHING + commit)--> send --> PUBLISHED
                                                       \\--> FAILED   (platform said no / not sent)
                                                       \\--> UNKNOWN  (timeout after sending)

The PUBLISHING state is committed BEFORE the external call. If the process
dies mid-send, the row is found in PUBLISHING on the next run and is NOT
re-sent blindly: the adapter's `find_existing()` is asked first (if it can
look the post up); otherwise the row becomes UNKNOWN and a human decides.
A duplicate post is worse than a delayed one.

Adapters shipped:
* `MockPublisher`      -- tests and local runs (can simulate failures)
* `ManualPublisher`    -- zero-cost default: the team copies the approved
                          text into WhatsApp and confirms with the link
* `TelegramBotPublisher` -- Telegram Bot API sendMessage to a channel where
                          the bot is an admin (configured by env vars)
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.errors import (
    AgentError,
    ConfigurationError,
    ErrorCategory,
    TransientError,
    category_for_http_status,
)
from app.agent.normalization import sha256_hex
from app.agent.observability import log_stage
from app.core.config import Settings
from app.core.config import settings as global_settings
from app.models.agent import AgentPostDraft, Publication
from app.models.agent_enums import PublicationStatus as P


@dataclasses.dataclass(frozen=True)
class PublishPayload:
    idempotency_key: str
    opportunity_id: uuid.UUID
    draft_id: uuid.UUID
    title: str
    text: str
    link: str | None
    target: str


@dataclasses.dataclass(frozen=True)
class PublishResult:
    external_id: str | None = None
    external_url: str | None = None
    manual: bool = False  # nothing was sent; a human must post and confirm


class PublishError(AgentError):
    """`delivered` tells whether the platform may have received the post:
    False -> safe to retry later; None -> unknown (never auto-retried)."""

    def __init__(self, message: str, *, code: str, category: ErrorCategory, delivered: bool | None):
        super().__init__(message, code=code, category=category)
        self.delivered = delivered


@runtime_checkable
class PublisherConnector(Protocol):
    name: str
    target: str

    async def publish(self, payload: PublishPayload) -> PublishResult: ...


def idempotency_key(opportunity_id: uuid.UUID, platform: str, target: str, draft_id: uuid.UUID) -> str:
    return sha256_hex(f"{opportunity_id}|{platform}|{target}|{draft_id}")


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------

class MockPublisher:
    """In-memory publisher. `fail_times` simulates platform failures."""

    name = "mock"

    def __init__(self, *, target: str = "default", fail_times: int = 0, uncertain: bool = False):
        self.target = target
        self.sent: list[PublishPayload] = []
        self._fail_times = fail_times
        self._uncertain = uncertain
        self._existing: dict[str, PublishResult] = {}

    async def publish(self, payload: PublishPayload) -> PublishResult:
        if self._fail_times > 0:
            self._fail_times -= 1
            if self._uncertain:
                raise PublishError("timeout after send", code="MOCK_TIMEOUT", category=ErrorCategory.TRANSIENT,
                                   delivered=None)
            raise PublishError("mock platform unavailable", code="MOCK_UNAVAILABLE",
                               category=ErrorCategory.EXTERNAL_SERVICE, delivered=False)
        self.sent.append(payload)
        result = PublishResult(external_id=f"mock-{len(self.sent)}", external_url=f"https://mock.local/{len(self.sent)}")
        self._existing[payload.idempotency_key] = result
        return result

    async def find_existing(self, key: str) -> PublishResult | None:
        return self._existing.get(key)


class ManualPublisher:
    name = "manual"

    def __init__(self, target: str = "whatsapp-community"):
        self.target = target

    async def publish(self, payload: PublishPayload) -> PublishResult:
        return PublishResult(manual=True)


class TelegramBotPublisher:
    """Telegram Bot API `sendMessage`. The bot must be an admin of the
    target channel. Plain text (no parse_mode) so user content can never
    break the request with markup errors."""

    name = "telegram"
    _API = "https://api.telegram.org"

    def __init__(self, *, token: str | None, chat_id: str | None, timeout_seconds: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not token or not chat_id:
            raise ConfigurationError("TELEGRAM_BOT_TOKEN and TELEGRAM_PUBLISH_CHAT_ID are required",
                                     code="PUBLISHER_NOT_CONFIGURED")
        self._token = token
        self.target = str(chat_id)
        self._timeout = timeout_seconds
        self._transport = transport

    async def publish(self, payload: PublishPayload) -> PublishResult:
        url = f"{self._API}/bot{self._token}/sendMessage"
        body = {"chat_id": self.target, "text": payload.text[:4096], "disable_web_page_preview": False}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(url, json=body)
        except httpx.ConnectError as exc:
            raise PublishError("telegram unreachable", code="TELEGRAM_CONNECT_ERROR",
                               category=ErrorCategory.TRANSIENT, delivered=False) from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            # The request may have reached Telegram: delivery unknown.
            raise PublishError("telegram timeout", code="TELEGRAM_TIMEOUT", category=ErrorCategory.TRANSIENT,
                               delivered=None) from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400 or not data.get("ok"):
            raise PublishError(
                f"telegram HTTP {response.status_code}: {str(data.get('description', ''))[:120]}",
                code=f"TELEGRAM_HTTP_{response.status_code}",
                category=category_for_http_status(response.status_code if response.status_code >= 400 else 502),
                delivered=False,
            )
        result = data.get("result") or {}
        message_id = result.get("message_id")
        username = (result.get("chat") or {}).get("username")
        link = f"https://t.me/{username}/{message_id}" if username and message_id else None
        return PublishResult(external_id=str(message_id) if message_id is not None else None, external_url=link)


def build_publishers(cfg: Settings | None = None) -> list[PublisherConnector]:
    cfg = cfg or global_settings
    publishers: list[PublisherConnector] = []
    for name in cfg.agent_publisher_names:
        if name == "mock":
            publishers.append(MockPublisher())
        elif name == "manual":
            publishers.append(ManualPublisher())
        elif name == "telegram":
            publishers.append(TelegramBotPublisher(token=cfg.telegram_bot_token, chat_id=cfg.telegram_publish_chat_id))
        else:
            raise ConfigurationError(f"unknown publisher {name!r}", code="PUBLISHER_UNKNOWN")
    return publishers


# --------------------------------------------------------------------------
# Idempotent publish
# --------------------------------------------------------------------------

@dataclasses.dataclass
class PublishOutcome:
    publication: Publication
    sent_now: bool  # True only if this call delivered the post


Commit = Callable[[], Awaitable[None]]


async def _get_or_create_publication(db: AsyncSession, draft: AgentPostDraft, publisher: PublisherConnector) -> Publication:
    key = idempotency_key(draft.opportunity_id, publisher.name, publisher.target, draft.id)
    existing = (await db.execute(select(Publication).where(Publication.idempotency_key == key))).scalars().first()
    if existing is not None:
        return existing
    publication = Publication(
        idempotency_key=key,
        draft_id=draft.id,
        opportunity_id=draft.opportunity_id,
        platform=publisher.name,
        target=publisher.target,
        status=P.PENDING.value,
        attempts=0,
    )
    try:
        async with db.begin_nested():
            db.add(publication)
            await db.flush()
    except IntegrityError:
        return (await db.execute(select(Publication).where(Publication.idempotency_key == key))).scalars().one()
    return publication


async def publish_draft(
    db: AsyncSession,
    draft: AgentPostDraft,
    publisher: PublisherConnector,
    *,
    title: str,
    link: str | None,
    commit: Commit,
) -> PublishOutcome:
    """Publish `draft` through `publisher` at most once. `commit` persists
    the PUBLISHING marker before the external call (crash safety)."""
    publication = await _get_or_create_publication(db, draft, publisher)
    status = P(publication.status)

    if status in (P.PUBLISHED, P.AWAITING_MANUAL):
        log_stage("publish_skipped_idempotent", publication_id=publication.id, status=status.value)
        return PublishOutcome(publication, sent_now=False)

    if status in (P.PUBLISHING, P.UNKNOWN):
        finder = getattr(publisher, "find_existing", None)
        found = await finder(publication.idempotency_key) if finder else None
        if found is not None:
            _mark_published(publication, found)
            await commit()
            log_stage("publish_reconciled", publication_id=publication.id)
            return PublishOutcome(publication, sent_now=False)
        publication.status = P.UNKNOWN.value
        publication.last_error = "previous attempt may have been delivered; refusing to send twice"
        await commit()
        raise PublishError("publication state unknown -- needs a human", code="PUBLICATION_STATE_UNKNOWN",
                           category=ErrorCategory.DATA, delivered=None)

    # PENDING or FAILED -> attempt
    publication.status = P.PUBLISHING.value
    publication.attempts += 1
    await commit()

    payload = PublishPayload(
        idempotency_key=publication.idempotency_key,
        opportunity_id=draft.opportunity_id,
        draft_id=draft.id,
        title=title,
        text=draft.body,
        link=link,
        target=publisher.target,
    )
    try:
        result = await publisher.publish(payload)
    except PublishError as exc:
        publication.status = P.UNKNOWN.value if exc.delivered is None else P.FAILED.value
        publication.last_error = exc.message[:2000]
        await commit()
        log_stage("publish_failed", publication_id=publication.id, error_code=exc.code, status=publication.status)
        if exc.delivered is None:
            raise PublishError(exc.message, code="PUBLICATION_STATE_UNKNOWN", category=ErrorCategory.DATA,
                               delivered=None) from exc
        raise
    except AgentError as exc:
        publication.status = P.FAILED.value
        publication.last_error = exc.message[:2000]
        await commit()
        raise
    except Exception as exc:  # noqa: BLE001 - unknown adapter failure: delivery unknown
        publication.status = P.UNKNOWN.value
        publication.last_error = f"{type(exc).__name__}"
        await commit()
        raise PublishError(f"publisher crashed: {type(exc).__name__}", code="PUBLICATION_STATE_UNKNOWN",
                           category=ErrorCategory.DATA, delivered=None) from exc

    if result.manual:
        publication.status = P.AWAITING_MANUAL.value
    else:
        _mark_published(publication, result)
    await commit()
    log_stage("publish_ok", publication_id=publication.id, platform=publisher.name, status=publication.status)
    return PublishOutcome(publication, sent_now=not result.manual)


def _mark_published(publication: Publication, result: PublishResult) -> None:
    publication.status = P.PUBLISHED.value
    publication.external_id = result.external_id
    publication.external_url = result.external_url
    publication.published_at = dt.datetime.now(dt.timezone.utc)
    publication.last_error = None


async def confirm_manual_publication(db: AsyncSession, publication: Publication, *, external_url: str | None,
                                     external_id: str | None = None) -> Publication:
    if publication.status not in (P.AWAITING_MANUAL.value, P.UNKNOWN.value):
        raise AgentError(f"publication is {publication.status}, not awaiting confirmation",
                         code="PUBLICATION_NOT_AWAITING", category=ErrorCategory.DATA)
    _mark_published(publication, PublishResult(external_id=external_id, external_url=external_url))
    await db.flush()
    return publication


__all__ = [
    "ManualPublisher",
    "MockPublisher",
    "PublishError",
    "PublishOutcome",
    "PublishPayload",
    "PublishResult",
    "PublisherConnector",
    "TelegramBotPublisher",
    "TransientError",
    "build_publishers",
    "confirm_manual_publication",
    "idempotency_key",
    "publish_draft",
]
