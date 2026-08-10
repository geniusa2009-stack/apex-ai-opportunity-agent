import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserHistory(Base):
    """Metadata row for one saved AI-generated artifact (an essay draft, a
    CV, a practice-question set, etc. -- anything any of Apex's Gemini/
    Claude-backed features can produce). This is Layer 1 of the 3-layer
    Enterprise Memory & Cloud Vault architecture introduced in this step:
    lightweight, fast-to-query metadata in Postgres, pointing at the
    actual (often large) JSON payload stored in Supabase Storage (Layer
    2, see app/services/storage_service.py) rather than embedding it
    directly in this table. See the README section for this step for the
    full 3-layer rationale.

    Deliberately its own explicit `created_at` column, NOT
    `TimestampMixin` (used by every other model in this codebase except
    UserBookmark) -- the ask's field list for this model names exactly
    `id`, `user_id`, `feature_key`, `title`, `storage_path`, `is_pinned`,
    `created_at`, with no `updated_at`. Followed literally here (same
    precedent as UserBookmark, the one other model that also only has
    `created_at`, app/models/bookmark.py) rather than defaulting to the
    mixin, since this ask was unusually precise about exact per-model
    fields compared to most prior asks in this project.
    """

    __tablename__ = "user_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Free text, not a Literal/Enum -- dozens of distinct Gemini/Claude
    # features already exist across this codebase (see
    # app/api/v1/router.py), each able to produce a savable artifact, and
    # new ones are added frequently. Hard-coding an enum here would need
    # editing every time a new feature is added; a plain indexed string
    # doesn't.
    feature_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)

    # The path/key into the `user-artifacts` Supabase Storage bucket,
    # e.g. `history/{user_id}/{artifact_id}.json` -- see
    # app/services/storage_service.py. Deliberately NEVER returned in any
    # API response (see HistoryOut in app/schemas/history.py) -- it's an
    # internal pointer, not something a caller needs, or should be able
    # to use to address Supabase Storage directly, bypassing this API's
    # own auth/ownership checks.
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)

    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<UserHistory id={self.id} user_id={self.user_id} "
            f"feature_key={self.feature_key!r}>"
        )
