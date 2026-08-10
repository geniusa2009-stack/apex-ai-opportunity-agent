"""Smart Notifications Engine -- transactional email dispatch.

DISCLOSED PATH NOTE: this step's own ask doesn't name a literal file
path for the Celery task itself (only the function name/signature,
`send_transactional_email_task(email_to, subject, body)`). Placed under
`app/workers/` -- this codebase's established, flat Celery-task-module
convention (see `app/workers/cleanup_tasks.py`'s own docstring for the
full history of this convention, including the one disclosed `app/
worker/tasks/` -> `app/workers/` deviation it already covers).

This task takes NO database session and does no async bridging at all --
same "doesn't touch the database" shape as `cleanup_tasks.
task_delete_orphaned_media` -- it is handed a fully-formed
`email_to`/`subject`/`body` by its one caller (`notification_service.
create_notification`) and its only job is getting that content sent
somewhere, non-blockingly (i.e. off the request/response path that
created the notification in the first place, per this step's own
literal "using... Celery... non-blocking" instruction).

MOCK VS. REAL SEND: see `app/core/config.py`'s own `smtp_host` comment
for the full reasoning -- this task checks `settings.smtp_host` at CALL
time (not import time), so flipping SMTP settings on/off between test
runs or environments doesn't require restarting a worker process with a
different code path baked in.
"""

import logging
import smtplib
from email.message import EmailMessage

from app.core.celery_app import celery_app
from app.core.config import settings

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.notification_tasks.send_transactional_email_task",
    # A real SMTP send is real network I/O against a third-party server
    # -- transient failures (a momentary DNS blip, the SMTP server
    # briefly refusing a connection) are a normal, expected occurrence,
    # not a reason to drop the email on the floor forever. Bounded to 3
    # attempts with a short fixed backoff -- an email notification is
    # not latency-critical enough to need `chat_connection_manager`'s
    # own uncapped-duration reconnect posture, but IS important enough
    # not to give up on the very first blip.
    autoretry_for=(smtplib.SMTPException, OSError),
    retry_kwargs={"max_retries": 3, "countdown": 10},
)
def send_transactional_email_task(email_to: str, subject: str, body: str) -> dict[str, str]:
    """Sends one transactional email, or logs a mock send if no real SMTP
    server is configured (see this module's own docstring). Returns a
    small status dict (`{"status": "sent" | "mocked", "email_to": ...}`)
    -- useful for anyone inspecting this task's own Celery result backend
    entry, not consumed by any caller in this codebase today.
    """
    if not settings.smtp_host or not settings.smtp_username or not settings.smtp_password or not settings.smtp_from_email:
        logger.info(
            "notifications: MOCK email send (no real SMTP configured) -- to=%s subject=%r body=%r",
            email_to, subject, body,
        )
        return {"status": "mocked", "email_to": email_to}

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from_email
    message["To"] = email_to
    message.set_content(body)

    # `SMTP_SSL`, not `SMTP` + `.starttls()` -- port 465 (this setting's
    # own default) is the implicit-TLS-from-connect-time convention most
    # modern transactional email providers (SendGrid, Postmark, SES,
    # Gmail) document as their primary recommended port; a provider that
    # instead requires STARTTLS on port 587 is a real, disclosed
    # follow-up this basic setup doesn't branch for, since the ask itself
    # asks for "a basic smtplib setup," not a multi-provider abstraction.
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port) as server:
        server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)

    logger.info("notifications: sent real email to=%s subject=%r", email_to, subject)
    return {"status": "sent", "email_to": email_to}
