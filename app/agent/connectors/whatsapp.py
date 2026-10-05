"""WhatsApp connectors -- isolated from the core.

The core only ever sees `IngestionEvent`s. Everything WhatsApp-specific
lives here, behind one small interface (`WhatsAppConnector`), so the
implementation can be swapped without touching the pipeline:

* ``cloud_api`` -- the official Meta WhatsApp Cloud API webhook
  (X-Hub-Signature-256 verified with the app secret). Realistic use: the
  APEX bot number receives messages that volunteers FORWARD to it in a 1:1
  chat. Meta's API does not let a business number join existing community
  groups created by personal accounts, so group monitoring is not claimed
  for this connector.
* ``bridge`` -- any self-hosted bridge that APEX operates for groups the
  APEX number legitimately belongs to POSTs already-normalized messages,
  signed with HMAC-SHA256 (X-Apex-Signature) and a timestamp (replay
  protection). No bridge is shipped: unofficial WhatsApp clients can break
  WhatsApp's terms and get numbers banned -- see WHATSAPP.md. Nothing here
  hides automation or evades limits.
* ``mock`` -- development/tests only (refused in production by config checks).

Gate (applied to every message, every connector):
1. bot-loop protection: `from_me` or a sender id in WHATSAPP_BOT_IDS -> ignored
2. malformed (no id / no content) -> refused
3. group message -> group id must be in ALLOWED_WHATSAPP_GROUP_IDS
4. direct message -> sender must be in ALLOWED_WHATSAPP_SENDER_IDS
Refused messages are never stored (privacy) -- only a counter is logged.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from app.agent.errors import AgentError, ErrorCategory
from app.agent.ingestion import AttachmentMeta, IngestionEvent
from app.models.enums import SourceType

MAX_BODY_BYTES = 1_000_000
REPLAY_WINDOW_SECONDS = 300


class SignatureError(AgentError):
    category = ErrorCategory.AUTHENTICATION

    def __init__(self, message: str = "invalid webhook signature"):
        super().__init__(message, code="WEBHOOK_SIGNATURE_INVALID")


class PayloadError(AgentError):
    category = ErrorCategory.DATA

    def __init__(self, message: str):
        super().__init__(message, code="WEBHOOK_PAYLOAD_INVALID")


class WhatsAppInboundMessage(BaseModel):
    """Normalized WhatsApp message (connector-independent)."""

    message_id: str = Field(min_length=1, max_length=255)
    sender_id: str | None = Field(default=None, max_length=255)
    sender_name: str | None = Field(default=None, max_length=255)
    group_id: str | None = Field(default=None, max_length=255)
    group_name: str | None = Field(default=None, max_length=255)
    text: str = Field(default="", max_length=20_000)
    links: list[str] = Field(default_factory=list, max_length=25)
    attachments: list[AttachmentMeta] = Field(default_factory=list, max_length=10)
    timestamp: dt.datetime | None = None
    from_me: bool = False


class GateDecision(str, enum.Enum):
    ACCEPTED = "accepted"
    IGNORED_SELF = "ignored_self"
    UNAUTHORIZED_GROUP = "unauthorized_group"
    UNAUTHORIZED_SENDER = "unauthorized_sender"
    MALFORMED = "malformed"


@dataclasses.dataclass(frozen=True)
class WhatsAppGate:
    allowed_group_ids: frozenset[str]
    allowed_sender_ids: frozenset[str]
    bot_ids: frozenset[str]

    @staticmethod
    def _norm_id(value: str | None) -> str:
        """'+20 100-123', '20100123@s.whatsapp.net' and '20100123' compare equal."""
        if not value:
            return ""
        base = value.split("@", 1)[0].strip()
        compact = base.lstrip("+").replace(" ", "").replace("-", "")
        return compact if compact.isdigit() else value.strip().lower()

    def _in(self, value: str | None, allowed: frozenset[str]) -> bool:
        key = self._norm_id(value)
        return bool(key) and key in {self._norm_id(a) for a in allowed}

    def evaluate(self, message: WhatsAppInboundMessage) -> GateDecision:
        if message.from_me or self._in(message.sender_id, self.bot_ids):
            return GateDecision.IGNORED_SELF
        if not message.text.strip() and not message.links and not message.attachments:
            return GateDecision.MALFORMED
        if message.group_id:
            return GateDecision.ACCEPTED if message.group_id in self.allowed_group_ids else GateDecision.UNAUTHORIZED_GROUP
        return GateDecision.ACCEPTED if self._in(message.sender_id, self.allowed_sender_ids) else GateDecision.UNAUTHORIZED_SENDER


def to_ingestion_event(message: WhatsAppInboundMessage) -> IngestionEvent:
    key = message.group_id or f"dm:{message.sender_id}"
    return IngestionEvent(
        source_type=SourceType.WHATSAPP,
        source_key=key,
        source_display_name=message.group_name or message.sender_name,
        source_base_url="https://www.whatsapp.com",
        external_message_id=message.message_id,
        sender_id=message.sender_id,
        sender_name=message.sender_name,
        group_id=message.group_id,
        group_name=message.group_name,
        text=message.text,
        links=message.links,
        attachments=message.attachments,
        received_at=message.timestamp,
        from_bot=message.from_me,
    )


class WhatsAppConnector(Protocol):
    name: str

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[WhatsAppInboundMessage]: ...


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _load_json(body: bytes) -> Any:
    if len(body) > MAX_BODY_BYTES:
        raise PayloadError("payload too large")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadError("payload is not valid JSON") from exc


# --------------------------------------------------------------------------
# Bridge (generic, HMAC-signed)
# --------------------------------------------------------------------------

def sign_bridge_payload(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class BridgeWhatsAppConnector:
    name = "bridge"

    def __init__(self, secret: str | None, *, now=time.time):
        if not secret:
            raise SignatureError("bridge secret not configured")
        self._secret = secret
        self._now = now

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[WhatsAppInboundMessage]:
        h = _lower(headers)
        signature, timestamp = h.get("x-apex-signature", ""), h.get("x-apex-timestamp", "")
        if not signature or not timestamp:
            raise SignatureError("missing signature headers")
        try:
            ts = int(timestamp)
        except ValueError as exc:
            raise SignatureError("bad timestamp") from exc
        if abs(self._now() - ts) > REPLAY_WINDOW_SECONDS:
            raise SignatureError("timestamp outside the replay window")
        expected = sign_bridge_payload(self._secret, timestamp, body)
        if not hmac.compare_digest(expected, signature):
            raise SignatureError()
        data = _load_json(body)
        raw = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raise PayloadError("expected {'messages': [...]}")
        return [_validate(item) for item in raw[:100]]


def _validate(item: Any) -> WhatsAppInboundMessage:
    try:
        return WhatsAppInboundMessage.model_validate(item)
    except ValidationError as exc:
        raise PayloadError(f"invalid message: {exc.errors()[0].get('msg')}") from exc


# --------------------------------------------------------------------------
# Official Meta WhatsApp Cloud API webhook
# --------------------------------------------------------------------------

class CloudApiWhatsAppConnector:
    name = "cloud_api"

    def __init__(self, app_secret: str | None, verify_token: str | None):
        if not app_secret or not verify_token:
            raise SignatureError("cloud api secrets not configured")
        self._secret = app_secret
        self._verify_token = verify_token

    def verify_subscription(self, params: Mapping[str, str]) -> str | None:
        """GET handshake: echo hub.challenge only for the right token."""
        if params.get("hub.mode") == "subscribe" and hmac.compare_digest(params.get("hub.verify_token", ""), self._verify_token):
            return params.get("hub.challenge", "")
        return None

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[WhatsAppInboundMessage]:
        signature = _lower(headers).get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(self._secret.encode(), body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            raise SignatureError()
        data = _load_json(body)
        messages: list[WhatsAppInboundMessage] = []
        for entry in (data.get("entry") or []) if isinstance(data, dict) else []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                business = (value.get("metadata") or {}).get("display_phone_number")
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
                for raw in value.get("messages") or []:  # status updates have no "messages"
                    parsed = _parse_cloud_message(raw, names, business)
                    if parsed is not None:
                        messages.append(parsed)
        return messages


def _parse_cloud_message(raw: dict, names: dict, business_number: str | None) -> WhatsAppInboundMessage | None:
    msg_type = raw.get("type")
    text = ""
    attachments: list[AttachmentMeta] = []
    if msg_type == "text":
        text = (raw.get("text") or {}).get("body", "")
    elif msg_type in ("image", "document", "video"):
        media = raw.get(msg_type) or {}
        attachments.append(AttachmentMeta(kind=msg_type, mime_type=media.get("mime_type"),
                                          file_name=media.get("filename"), caption=media.get("caption")))
    else:
        return None  # reactions, stickers, locations... are not opportunities
    sender = raw.get("from")
    timestamp = None
    if str(raw.get("timestamp", "")).isdigit():
        timestamp = dt.datetime.fromtimestamp(int(raw["timestamp"]), tz=dt.timezone.utc)
    try:
        return WhatsAppInboundMessage(
            message_id=raw.get("id") or "",
            sender_id=sender,
            sender_name=names.get(sender),
            text=text,
            attachments=attachments,
            timestamp=timestamp,
            from_me=bool(business_number and sender and sender.lstrip("+") == str(business_number).lstrip("+")),
        )
    except ValidationError:
        return None


class MockWhatsAppConnector:
    """Unsigned, for local development and tests only."""

    name = "mock"

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[WhatsAppInboundMessage]:
        data = _load_json(body)
        raw = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raise PayloadError("expected {'messages': [...]}")
        return [_validate(item) for item in raw[:100]]


def build_whatsapp_connector(cfg) -> WhatsAppConnector | None:
    kind = (cfg.whatsapp_connector or "disabled").lower()
    if kind == "disabled":
        return None
    if kind == "mock":
        return MockWhatsAppConnector()
    if kind == "bridge":
        return BridgeWhatsAppConnector(cfg.whatsapp_bridge_hmac_secret)
    if kind == "cloud_api":
        return CloudApiWhatsAppConnector(cfg.whatsapp_cloud_app_secret, cfg.whatsapp_cloud_verify_token)
    raise AgentError(f"unknown WhatsApp connector {kind!r}", code="WHATSAPP_UNKNOWN_CONNECTOR",
                     category=ErrorCategory.AUTHENTICATION)


def build_gate(cfg) -> WhatsAppGate:
    return WhatsAppGate(
        allowed_group_ids=cfg.allowed_whatsapp_group_id_set,
        allowed_sender_ids=cfg.allowed_whatsapp_sender_id_set,
        bot_ids=cfg.whatsapp_bot_id_set,
    )
