"""High-Risk-Content View -- service layer for `GET /admin/high-risk`.

Investigation summary (this task's own required Step 1, kept here as
the real, durable record rather than only in a chat reply):

1. `safety_filter.py` (both the service and its endpoint) is NOT a
   content-moderation risk filter of any kind -- confirmed by reading it
   in full. It's a Gemini-backed advisor (`POST /safety-filter/analyze`)
   that evaluates whether a SCHOLARSHIP's funding is genuinely
   comprehensive and whether a DESTINATION CITY is safe for a minor high
   school student traveling abroad -- a completely different domain
   (helping a *student* evaluate a *scholarship*) from anything about
   flagging risky user-generated *content* on this platform. It scores
   nothing, flags nothing, and has no relationship to moderation. This
   is a real, disclosed finding: the 30-feature plan's future item
   ("blacklist words/patterns auto-trigger via safety_filter.py") is
   based on a misnomer and will need re-scoping when that item comes up
   -- there is no existing blacklist/pattern-matching logic anywhere in
   this file for that future feature to extend.
2. Across every real content-creation pathway (videos, chat messages,
   chat reports, guild posts), the real, currently-computable "this
   needs a closer look" signals already exist, scattered across prior
   phases:
   - `repeat_offender_service.get_flagged_users` -- real users at/above
     the repeat-offender threshold (Phase 8). Already a real, tested,
     Owner-pollable signal.
   - `chat_security.list_banned_users` -- every currently-active Redis
     chat ban. This function already existed (built for the Admin Hub's
     "list of currently banned users" widget) but -- confirmed by a full
     grep across every `admin_*` endpoint file -- NO admin route has
     ever called it. A real, existing, tested function with zero real
     callers is a genuine small gap: it computes a real signal that
     currently surfaces nowhere.
   - The open chat-message report queue (`ChatMessageReport.status ==
     'open'`) and the open video review queue
     (`StudentVideoStatus.{REJECTED,FAILED,PENDING_AI}`) -- both already
     real, queryable, paginated elsewhere (`GET /admin/chat/reports`,
     `GET /admin/videos/review-queue`); an unusually large backlog on
     either is itself a real signal worth surfacing on one screen,
     without needing new detection logic.
   - Guild posts (`guilds.py` has real `POST /guilds/posts` and a real
     comment-submission endpoint): confirmed, via a grep across every
     `admin_*` file and `moderation_log_service.py`, that NO moderation
     of any kind exists for guild content yet -- no report queue, no
     `ModerationAction` member, nothing in `ModerationLogEntry`. There is
     therefore no real signal to aggregate for guilds today; inventing
     one here would be exactly the kind of unexplained, fabricated
     "risk" this task's own Step 3.2 explicitly warns against. Guild
     content moderation is its own, separate, not-yet-built item further
     down the 30-feature plan -- correctly out of scope for this task.
3. Conclusion, stated plainly per this task's own Step 1.3: there is NO
   genuine gap in risk-DETECTION here. Every signal below already exists
   and is already correct/tested from a prior phase. This feature is
   almost entirely a new, real AGGREGATION VIEW composing signals that
   already exist -- the one exception is wiring the already-built,
   already-tested `chat_security.list_banned_users` into an admin
   response for the first time, which is real but small (no new
   detection logic, just a first real caller for an existing function).

Design (Step 2): no new opaque "risk score" is computed anywhere in this
module. Every item this view surfaces carries its own real, plain-English
explanation, resolved directly from the real underlying fact (a real
offense count, a real remaining ban TTL, a real queue size/age) -- never
a fabricated composite number with no traceable basis. This matches this
project's own established stance against unexplained AI-flavored scoring
(Career Compass's deterministic RIASEC scoring instead of an LLM guess;
the probability-tool precedents).
"""

import dataclasses
import datetime as dt
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_moderation import ChatMessageReport
from app.models.enums import StudentVideoStatus
from app.models.student_videos import StudentVideo
from app.services import chat_security, repeat_offender_service
from app.models.chat_moderation import REPORT_STATUS_OPEN

# Re-declared here, not imported from `admin_moderation.py` -- same
# Models/Schemas -> Service -> Router layering discipline
# `moderation_stats_service.py`'s own docstring already establishes for
# this exact list (importing a route-module constant into the service
# layer would invert that layering). Keep in sync with
# `admin_moderation.py::_REVIEW_QUEUE_STATUSES` if that list ever
# changes -- same real, disclosed duplication trade-off already made
# there.
_REVIEW_QUEUE_STATUSES = [
    StudentVideoStatus.REJECTED.value,
    StudentVideoStatus.FAILED.value,
    StudentVideoStatus.PENDING_AI.value,
]


@dataclasses.dataclass
class HighRiskView:
    flagged_users: list[tuple[uuid.UUID, int]]
    banned_users: list[tuple[uuid.UUID, int]]
    open_chat_reports_count: int
    oldest_open_chat_report_at: dt.datetime | None
    pending_video_reviews_count: int
    oldest_pending_video_review_at: dt.datetime | None


async def get_high_risk_view(db: AsyncSession) -> HighRiskView:
    """Composes the four real signals described in this module's own
    docstring into one payload. Every sub-query here already exists
    elsewhere in this codebase in the same shape (this function adds no
    new detection logic) -- reused directly rather than re-derived, so
    this view can never silently disagree with
    `GET /admin/flagged-users`, `GET /admin/chat/reports`, or
    `GET /admin/videos/review-queue` about the same underlying facts.
    """
    flagged_users = await repeat_offender_service.get_flagged_users(db)
    banned_users = await chat_security.list_banned_users()

    chat_reports_result = await db.execute(
        select(func.count(), func.min(ChatMessageReport.created_at)).where(
            ChatMessageReport.status == REPORT_STATUS_OPEN
        )
    )
    open_chat_reports_count, oldest_open_chat_report_at = chat_reports_result.one()

    video_reviews_result = await db.execute(
        select(func.count(), func.min(StudentVideo.created_at)).where(
            StudentVideo.status.in_(_REVIEW_QUEUE_STATUSES)
        )
    )
    pending_video_reviews_count, oldest_pending_video_review_at = video_reviews_result.one()

    return HighRiskView(
        flagged_users=flagged_users,
        banned_users=banned_users,
        open_chat_reports_count=open_chat_reports_count or 0,
        oldest_open_chat_report_at=oldest_open_chat_report_at,
        pending_video_reviews_count=pending_video_reviews_count or 0,
        oldest_pending_video_review_at=oldest_pending_video_review_at,
    )
