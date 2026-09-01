"""General Content Blacklist / Pattern Auto-Trigger -- admin endpoints.

Step 3.4's own tier question, answered with reasoning: EDITING the
blacklist itself (add/remove patterns) is Owner-only -- a real, platform-
wide content policy decision (what words are banned across the ENTIRE
platform, every content type at once), the same class of decision the
Real Tiered Role System already reserves for Owner (role management,
platform-wide aggregate views like `GET /admin/high-risk`), not a
routine reviewable/reversible per-case action a Moderator handles day to
day. VIEWING the pattern list itself is kept at the same Owner-only tier
as editing it -- there is no real use case for a Moderator to see the
raw word list (they don't act on it directly; the filter runs
automatically), and showing it more broadly than necessary only widens
the audience that could learn exactly what to type to evade it.

VIEWING TRIGGER EVENTS (`GET /admin/blacklist/triggers`) is deliberately
Moderator+, not Owner-only -- these are real, already-happened
moderation-relevant events (a student's content got rejected somewhere
on the platform), the same kind of thing a Moderator already sees
inline in every review queue this codebase has; withholding "what's been
triggered, and by whom" from the people actually reviewing content day
to day would defeat the point of even having automated detection. This
is the real answer to Step 3.4's own explicit question.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser, DbSession, RequireModerationAccess, RequireOwner
from app.schemas.blacklist import (
    BlacklistedPatternCreate,
    BlacklistedPatternOut,
    BlacklistTriggerListResponse,
    BlacklistTriggerOut,
)
from app.services import blacklist_service

router = APIRouter(tags=["admin-blacklist"])


@router.get("/admin/blacklist/patterns", response_model=list[BlacklistedPatternOut])
async def list_blacklist_patterns(db: DbSession, _: RequireOwner) -> list[BlacklistedPatternOut]:
    patterns = await blacklist_service.list_patterns(db)
    return [BlacklistedPatternOut.model_validate(p) for p in patterns]


@router.post(
    "/admin/blacklist/patterns",
    response_model=BlacklistedPatternOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_blacklist_pattern(
    payload: BlacklistedPatternCreate, current_user: CurrentUser, db: DbSession, _: RequireOwner
) -> BlacklistedPatternOut:
    row = await blacklist_service.create_pattern(
        db,
        pattern=payload.pattern,
        is_regex=payload.is_regex,
        description=payload.description,
        created_by=current_user.id,
    )
    return BlacklistedPatternOut.model_validate(row)


@router.delete("/admin/blacklist/patterns/{pattern_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_blacklist_pattern(pattern_id: uuid.UUID, db: DbSession, _: RequireOwner) -> None:
    try:
        await blacklist_service.delete_pattern(db, pattern_id)
    except blacklist_service.PatternNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pattern not found") from exc


@router.get("/admin/blacklist/triggers", response_model=BlacklistTriggerListResponse)
async def list_blacklist_triggers(
    db: DbSession,
    _: RequireModerationAccess,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> BlacklistTriggerListResponse:
    entries, total = await blacklist_service.list_trigger_events(db, limit=limit, offset=offset)
    items = [
        BlacklistTriggerOut(
            id=e.id,
            target_user_id=e.target_id,
            content_type=e.outcome.get("content_type", ""),
            matched_pattern=e.outcome.get("matched_pattern", ""),
            content_snippet=e.outcome.get("content_snippet", ""),
            created_at=e.created_at,
        )
        for e in entries
    ]
    return BlacklistTriggerListResponse(items=items, total=total, limit=limit, offset=offset)
