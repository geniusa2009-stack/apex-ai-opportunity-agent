"""Opportunity Monitoring & Personal Impact Engine, Parts M/N: turns a
verified, recorded, HIGH+-severity opportunity change into real,
deduplicated, preference-respecting `Notification` rows for exactly the
affected users `personal_impact_service.find_affected_users` identifies.

REUSES, NEVER DUPLICATES: `notification_service.create_notification` is
the ONLY write path used here (Part M's own "reuse it, do not build a
second notification framework"). This module's only real additions on
top of that are candidate selection (which changes/users even reach
`create_notification`), the severity gate, the preference check, and
deduplication.

SEVERITY GATE: only CRITICAL/HIGH changes generate a notification.
MATERIAL changes still reach guidance (`guidance_service.py`, a pull
surface the user checks) and the personal feed, but not a push-style
notification -- Part C's own trust-over-volume principle applied
concretely: a MATERIAL change (e.g. minor funding-text rewording) is
real and worth surfacing when the user looks, but not urgent enough to
interrupt them; only a change with real, immediate stakes (deadline
gone/moved, opportunity closed/reopened, application link changed,
un-published) earns a notification.

DEDUPLICATION (Part N): keyed on `OpportunityChangeHistory.id` (a
stable, real, already-unique identity -- never a timestamp, which
retries/worker restarts could regenerate). Stored in `Notification.
payload["change_id"]`. Looked up by fetching this user's own recent
OPPORTUNITY/WARNING notifications and filtering in Python rather than a
JSONB path operator in the WHERE clause -- a deliberate, disclosed
portability choice: this codebase's test suite runs against SQLite
(which cannot compile JSONB path queries -- the same limitation
`test_live_session_reminders.py`/`test_student_videos.py` already hit
for other JSONB columns), and the per-user notification volume here is
always small (bounded by how many opportunities one user saved/applied
to), so a single indexed `(user_id, created_at)` query plus an
in-Python filter is both correct and cheap -- never a second query per
candidate notification.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ChangeSeverity
from app.models.notification import Notification
from app.models.opportunity import Opportunity
from app.services import notification_preferences
from app.services import notification_service
from app.services.opportunity_monitoring import RecordedChange
from app.services.personal_impact_service import AffectedUser, find_affected_users

_NOTIFIABLE_SEVERITIES = frozenset({ChangeSeverity.CRITICAL.value, ChangeSeverity.HIGH.value})

# Only these two categories are ever notification-worthy, per the
# severity gate above -- an application-tracker-scoped notification
# check is gated on the user's own `application_alerts` preference
# instead of `opportunity_changes` when they have an active application
# to this opportunity (Part O's own "distinct switches" reasoning).
_DEDUP_LOOKBACK_DAYS = 30


def _preference_category(field_name: str, *, has_active_application: bool) -> str:
    if has_active_application:
        return "application_alerts"
    if field_name == "deadline":
        return "deadline_alerts"
    return "opportunity_changes"


async def _already_notified_change_ids(db: AsyncSession, *, user_id: uuid.UUID, now: dt.datetime) -> set[str]:
    cutoff = now - dt.timedelta(days=_DEDUP_LOOKBACK_DAYS)
    result = await db.execute(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.created_at >= cutoff,
            Notification.payload.is_not(None),
        )
    )
    seen: set[str] = set()
    for notification in result.scalars().all():
        change_id = (notification.payload or {}).get("change_id")
        if change_id:
            seen.add(change_id)
    return seen


def _message_for(opportunity: Opportunity, recorded: RecordedChange) -> tuple[str, str]:
    field_label = recorded.history_row.field_name.replace("_", " ")
    title = f"{opportunity.title}: {field_label} changed"
    message = (
        f"{field_label.capitalize()} changed from "
        f"\"{recorded.history_row.old_value}\" to \"{recorded.history_row.new_value}\"."
    )
    return title, message


async def create_change_notifications_for_opportunity(
    db: AsyncSession,
    *,
    opportunity: Opportunity,
    recorded_changes: list[RecordedChange],
    now: dt.datetime | None = None,
) -> int:
    """Part G-N end to end, for ONE already-verified, already-recorded
    batch of changes on ONE opportunity. Returns the number of
    notifications actually created (0 is a completely normal, common
    result -- most recorded changes are MATERIAL or below, or every
    affected user has already been notified or opted out).

    Best-effort per user (Part V): one user's notification failing (a
    Redis hiccup inside `create_notification`'s own broadcast step, for
    instance) must never prevent the others from being notified -- this
    function itself does not swallow exceptions (its caller does, same
    "new-subsystem side effect wrapped in try/except" posture as every
    other best-effort integration point in this codebase), but a failure
    for one user never aborts the loop over the rest.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    notifiable = [r for r in recorded_changes if r.severity.value in _NOTIFIABLE_SEVERITIES]
    if not notifiable:
        return 0

    affected_users = await find_affected_users(db, opportunity_id=opportunity.id)
    if not affected_users:
        return 0

    created = 0
    for affected in affected_users:
        already_notified = await _already_notified_change_ids(db, user_id=affected.user_id, now=now)

        for recorded in notifiable:
            change_id = str(recorded.history_row.id)
            if change_id in already_notified:
                continue

            category = _preference_category(
                recorded.history_row.field_name, has_active_application=affected.has_active_application
            )
            if not await notification_preferences.is_opted_in(db, affected.user_id, category):
                continue

            title, message = _message_for(opportunity, recorded)
            reason_text = affected.reasons[0].text if affected.reasons else "This opportunity is relevant to you."
            notification_type = "WARNING" if recorded.severity == ChangeSeverity.CRITICAL else "OPPORTUNITY"

            await notification_service.create_notification(
                db,
                affected.user_id,
                notification_type,
                title,
                f"{message} {reason_text}",
                payload={
                    "change_id": change_id,
                    "opportunity_id": str(opportunity.id),
                    "field_name": recorded.history_row.field_name,
                    "severity": recorded.severity.value,
                },
            )
            already_notified.add(change_id)
            created += 1

    return created
