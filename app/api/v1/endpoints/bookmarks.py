from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DbSession
from app.models.bookmark import UserBookmark
from app.models.opportunity import Opportunity
from app.schemas.bookmark import BookmarkOut
from app.schemas.opportunity import PaginatedResponse

router = APIRouter(prefix="/bookmarks", tags=["bookmarks"])


@router.post(
    "/{opportunity_id}", response_model=BookmarkOut, status_code=status.HTTP_201_CREATED
)
async def create_bookmark(
    opportunity_id: UUID, current_user: CurrentUser, db: DbSession
) -> UserBookmark:
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    existing = await db.execute(
        select(UserBookmark).where(
            UserBookmark.user_id == current_user.id,
            UserBookmark.opportunity_id == opportunity_id,
        )
    )
    bookmark = existing.scalar_one_or_none()
    if bookmark is not None:
        # Bookmarking something already bookmarked is a no-op, not an
        # error -- return the existing row rather than a 409.
        return bookmark

    bookmark = UserBookmark(user_id=current_user.id, opportunity_id=opportunity_id)
    db.add(bookmark)
    try:
        await db.commit()
    except IntegrityError:
        # Two requests racing to bookmark the same opportunity between our
        # check above and this commit both pass the check, then only one
        # commit wins against the `uq_user_opportunity_bookmark` constraint
        # (Step 1.2). Roll back and fetch the winner instead of surfacing
        # a 500 for what is, semantically, still a no-op.
        await db.rollback()
        existing = await db.execute(
            select(UserBookmark).where(
                UserBookmark.user_id == current_user.id,
                UserBookmark.opportunity_id == opportunity_id,
            )
        )
        bookmark = existing.scalar_one()
    else:
        await db.refresh(bookmark)

    return bookmark


@router.get("/", response_model=PaginatedResponse[BookmarkOut])
async def list_bookmarks(
    current_user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[BookmarkOut]:
    total = (
        await db.execute(
            select(func.count())
            .select_from(UserBookmark)
            .where(UserBookmark.user_id == current_user.id)
        )
    ).scalar_one()

    result = await db.execute(
        select(UserBookmark)
        .where(UserBookmark.user_id == current_user.id)
        .order_by(UserBookmark.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    bookmarks = result.scalars().all()

    return PaginatedResponse[BookmarkOut](items=bookmarks, total=total, limit=limit, offset=offset)
