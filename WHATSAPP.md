# WhatsApp integration

## Principles

* APEX uses a **dedicated WhatsApp number** for the bot — never someone's
  personal number.
* The agent processes only chats APEX **legitimately belongs to**, listed in
  allowlists. Everything else is refused and **not stored**.
* No access bypass, no "anti-ban" tricks, no hiding automation, no spam.
* The connector is replaceable: the core only sees `IngestionEvent`s.

## Options compared (checked October 2026 — re-check before deciding)

| Option | Groups? | Cost | Risk |
|---|---|---|---|
| **Official WhatsApp Cloud API** (`cloud_api`) | Its Groups API only works with groups the business number *creates*, for Official Business Accounts, with small participant limits; it cannot join existing community groups made by personal accounts. Receiving 1:1 messages works. | Free tier for user-initiated conversations varies by Meta's pricing; business-initiated messages are paid. | Low (official). |
| **Self-hosted bridge** on the APEX number (`bridge`) | Yes, for groups the number is a member of. | Hosting only. | Unofficial clients (e.g. Baileys, whatsapp-web.js) can break WhatsApp's Terms and get the number banned. Your decision; not shipped here. |
| **Manual forwarding** (recommended start) | Volunteers forward opportunities 1:1 to the APEX number (Cloud API) or submit via `POST /api/v1/agent/submissions`. | Free. | None. |

Recommended path at 0 EGP: start with **volunteer submissions + manual
forwarding to the APEX number**, publish with the **manual publisher**
(the team pastes the approved text into the WhatsApp community and confirms
the link). Add a bridge only if the team accepts its risk.

## Configuration

```
WHATSAPP_CONNECTOR=cloud_api            # disabled | mock | cloud_api | bridge
ALLOWED_WHATSAPP_GROUP_IDS=120363...@g.us,120363...@g.us   # group messages
ALLOWED_WHATSAPP_SENDER_IDS=201001234567,201009876543        # 1:1 messages
WHATSAPP_BOT_IDS=201000000999           # the bot's own number(s)
WHATSAPP_CLOUD_APP_SECRET=...           # cloud_api: verifies X-Hub-Signature-256
WHATSAPP_CLOUD_VERIFY_TOKEN=...         # cloud_api: GET handshake
WHATSAPP_BRIDGE_HMAC_SECRET=...         # bridge: verifies X-Apex-Signature
```

Startup refuses `mock` in production and refuses `cloud_api`/`bridge`
without their secrets. Empty allowlists are allowed but logged as a warning
(every message would be refused).

Webhook URL: `POST https://<api>/api/v1/agent/whatsapp/webhook`
(Cloud API verification: `GET` on the same URL).

## Message gate (every connector)

1. `from_me` or sender in `WHATSAPP_BOT_IDS` → ignored (loop protection;
   ingestion double-checks `from_bot`).
2. No text, links or attachments → refused.
3. Group message → group id must be allowlisted.
4. Direct message → sender must be allowlisted.
5. Same message id from the same chat → stored once (DB unique constraint).

Phone numbers compare normalized (`+20 100-123…`, `20100123…@s.whatsapp.net`
and `20100123…` are equal). Logs mask phone numbers to the last 4 digits.

## Bridge contract (`bridge`)

```
POST /api/v1/agent/whatsapp/webhook
X-Apex-Timestamp: <unix seconds>                  (±300 s replay window)
X-Apex-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>." + raw_body)>
Content-Type: application/json

{"messages": [{
  "message_id": "3EB0...", "sender_id": "2010...@s.whatsapp.net", "sender_name": "Sara",
  "group_id": "120363...@g.us", "group_name": "APEX Scholarships",
  "text": "...", "links": ["https://..."],
  "attachments": [{"kind": "image", "mime_type": "image/jpeg", "caption": "..."}],
  "timestamp": "2026-10-05T09:00:00Z", "from_me": false
}]}
```

Media bytes are never sent or stored — metadata and captions only.

## Testing without WhatsApp

`WHATSAPP_CONNECTOR=mock` accepts the same JSON unsigned (development only).
Tests: `tests/test_agent_pipeline_e2e.py` (gate, bridge signatures and
replay, Cloud API parsing/signature/handshake, end-to-end flow).
