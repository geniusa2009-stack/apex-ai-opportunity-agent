"""Security Shield: Rate Limiting & Strict File Validation step --
database-backed (no Redis) FastAPI dependency enforcing a per-user daily
cap on Student Video uploads, to protect the AI moderation pipeline
(Whisper + Gemini calls, real per-video cost) and Cloudflare R2 storage
from spam/DoS.

DISCLOSED PLACEMENT: this codebase's other cross-cutting request
dependencies (`CurrentUser`, `DbSession`, `require_tokens`, `RequireAdmin`)
all live in `app/api/deps.py` -- no `app/api/dependencies/` package
existed anywhere in this codebase before this step. This step's own ask
explicitly names `app/api/dependencies/rate_limiter.py` as its target
path, though, so that's honored literally (a new, real importable
package, `app/api/dependencies/__init__.py` added alongside this file)
rather than folded into the existing `deps.py`. A dedicated
rate-limiting concern is a reasonable seed for that package if more
per-endpoint rate limiters are added later.

DISCLOSED DEVIATION FROM THE ASK'S LITERAL SIGNATURE: the ask specifies
`check_upload_rate_limit(current_user: User, db: Session = Depends(get_db))`
-- a SYNCHRONOUS SQLAlchemy `Session`. This entire codebase, without a
single exception, uses SQLAlchemy 2.0's ASYNC ORM (`AsyncSession`, `await
db.execute(...)`, `app.db.session.get_db` yielding an `AsyncSession`) --
see `app/api/deps.py`'s own `DbSession = Annotated[AsyncSession,
Depends(get_db)]`. A synchronous `Session` type does not match this
codebase's actual database engine/session factory at all; using it here
would not just be a style mismatch, it would not work (`get_db` yields an
`AsyncSession`, not a `sqlalchemy.orm.Session`). This dependency is
therefore written as `async def check_upload_rate_limit(current_user:
CurrentUser, db: DbSession) -> None`, reusing this codebase's own
established `CurrentUser`/`DbSession` `Annotated` aliases exactly the way
every other dependency here already does -- functionally identical to
the ask's intent (inject the current user and a DB session, check a
count, raise on violation), just matching this codebase's real,
established async convention instead of a synchronous one that doesn't
exist anywhere in it.

DISCLOSED SCOPE OF "successfully submitted": counts EVERY `StudentVideo`
row created for this user in the rolling 24-hour window, REGARDLESS of
its current `status` (`PENDING_AI`/`APPROVED`/`REJECTED`/`FAILED` all
count). A row is only ever created by this endpoint once a multipart
upload has been fully received and validated -- by the time a row exists,
this student has already consumed one real AI-moderation pass (a Whisper
transcription call + a Gemini evaluation call), which is exactly the
resource this limit exists to protect, per this step's own stated
rationale ("to ensure high-quality AI moderation"). A video that was
later REJECTED or FAILED still cost the same moderation-pipeline
resources as one that was APPROVED, so excluding non-approved rows from
the count would undercount the actual load a spammy user places on the
pipeline.
"""

import datetime as dt
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.models.student_videos import StudentVideo

# This step's own literal limit/window.
MAX_UPLOADS_PER_ROLLING_WINDOW = 3
RATE_LIMIT_WINDOW = dt.timedelta(hours=24)


async def check_upload_rate_limit(current_user: CurrentUser, db: DbSession) -> None:
    """Raises `429 Too Many Requests` if `current_user` has already
    submitted `MAX_UPLOADS_PER_ROLLING_WINDOW` (3) Student Videos within
    the last `RATE_LIMIT_WINDOW` (24 hours), counted as a true rolling
    window (`created_at >= now - 24h`), not a calendar-day reset -- a
    user who uploads 3 videos at 11:59pm cannot upload a 4th at 12:01am;
    they must wait until the OLDEST of those 3 uploads is itself more
    than 24 hours old. This matches the ask's own literal wording
    ("in the last 24 hours") more precisely than a UTC-midnight reset
    would.

    Uses `timezone.utc`-aware `datetime.now(...)`, not `datetime.utcnow()`
    -- disclosed deviation from the ask's own literal
    `datetime.utcnow() - timedelta(days=1)` wording: `datetime.utcnow()`
    is deprecated as of Python 3.12 and, more importantly, returns a
    NAIVE datetime, while `StudentVideo.created_at` is a genuinely
    timezone-aware column (`DateTime(timezone=True)`, Postgres `timestamptz`)
    -- comparing a naive Python datetime against that column is exactly
    the kind of naive/aware mismatch this codebase's own established
    convention (`datetime.now(timezone.utc)`, used uniformly across
    `coach_service.py`/`deadline_alerts.py`/`course_service.py`/
    `focus_service.py`) exists to avoid.
    """
    window_start = dt.datetime.now(dt.timezone.utc) - RATE_LIMIT_WINDOW

    upload_count = (
        await db.execute(
            select(func.count())
            .select_from(StudentVideo)
            .where(
                StudentVideo.student_id == current_user.id,
                StudentVideo.created_at >= window_start,
            )
        )
    ).scalar_one()

    if upload_count >= MAX_UPLOADS_PER_ROLLING_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Daily upload limit reached. You can only upload "
                f"{MAX_UPLOADS_PER_ROLLING_WINDOW} videos per 24 hours to "
                "ensure high-quality AI moderation."
            ),
        )


# `Annotated[None, Depends(...)]` alias, same established pattern as
# `app/api/deps.py`'s own `RequireAdmin = Annotated[None, Depends(require_admin)]`
# -- lets router modules declare this as a plain, no-default parameter
# (`_rate_limit_check: CheckUploadRateLimit`) alongside `CurrentUser`/
# `DbSession`, rather than needing a `= Depends(...)` default value that
# would force it to the end of the parameter list.
CheckUploadRateLimit = Annotated[None, Depends(check_upload_rate_limit)]
