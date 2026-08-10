import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import SourceType
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.opportunity import Opportunity


class Source(Base, TimestampMixin):
    """A site/feed we pull opportunities from -- a scraped website
    (`app/scraper/pipelines.py`'s `PostgresPipeline`), or, as of the
    Hybrid Opportunity Aggregation Engine step, a Telegram channel or
    Facebook page (`app/services/aggregator_service.py`).

    Hybrid Opportunity Aggregation Engine addition: `source_type`. That
    step's own ask lists `source_name`/`source_type` as `Opportunity`
    fields, but this codebase already has exactly one normalized place a
    "what feed did this come from, and what kind of feed is it" fact
    belongs -- this table, which every `Opportunity` already FKs into via
    `source_id`. Adding a second, flat `source_type` string directly onto
    `Opportunity` (duplicating what's already knowable through
    `opportunity.source.source_type`) would create two sources of truth
    for the same fact with no mechanism keeping them in sync -- a real
    data-integrity risk this codebase's existing FK relationship already
    prevents. `source_name` similarly isn't duplicated onto `Opportunity`
    -- `Source.name` (already present, used by the existing scraper
    pipeline) already serves that exact purpose. Both concepts are
    exposed to API consumers via `SourceOut.source_type`/`SourceOut.name`
    (app/schemas/opportunity.py), nested under `OpportunityOut.source`
    exactly the way `SourceOut.name` already was before this step.
    """

    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    active_status: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    # Plain String column backed by the `SourceType` Python enum (app/models/enums.py)
    # -- same "not a native Postgres ENUM type" posture as `StudentVideo.status`/
    # `Lesson.video_processing_status`, since this step's ask writes
    # `source_type: String (Enum: ...)` rather than asking for a native
    # enum column type the way `Opportunity.type` (`OpportunityType`) is.
    # `server_default=SourceType.WEBSITE.value` -- every `Source` row that
    # exists BEFORE this migration was created by the Scrapy-based website
    # scraper (`app/scraper/pipelines.py`), so "website" is the objectively
    # correct value for every pre-existing row, not an arbitrary filler
    # default -- no separate backfill script is needed as a result.
    source_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SourceType.WEBSITE.value, server_default=SourceType.WEBSITE.value
    )

    opportunities: Mapped[list["Opportunity"]] = relationship(
        back_populates="source",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Source id={self.id} name={self.name!r}>"
