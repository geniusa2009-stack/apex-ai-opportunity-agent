import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    pass


class Alumnus(Base, TimestampMixin):
    """A past scholarship/opportunity winner, kept on file so current
    students can be matched with someone who's already been through the
    same (or a similar) process, for networking/mentorship purposes.

    Added for Step 3.22 / Advanced Feature 8 (AI Alumni Matchmaker). This
    table didn't exist before this step -- there was nothing for the
    router's "query the database for alumni" instruction to query against.
    See the README section for this step for the three options presented
    and which one was chosen (a new, real, persistent table).
    """

    __tablename__ = "alumni"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # All self-reported/admin-entered profile fields below are nullable --
    # same trust model as User's profile fields (Step 3.20): nothing here
    # is verified against an external source, and a given alumnus record
    # may not have every field filled in.
    university: Mapped[str | None] = mapped_column(String(255), nullable=True)
    major: Mapped[str | None] = mapped_column(String(255), nullable=True)
    country: Mapped[str | None] = mapped_column(String(120), nullable=True)
    degree_level: Mapped[str | None] = mapped_column(
        String(100), nullable=True, doc="Free text, e.g. 'Undergraduate', 'Master's', 'PhD'."
    )
    current_position: Mapped[str | None] = mapped_column(
        String(500), nullable=True, doc="E.g. 'Software Engineer at Google'."
    )
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)

    linkedin_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The specific opportunity/scholarship this alumnus won, if on record.
    # Nullable + ondelete="SET NULL" (not CASCADE, unlike
    # UserBookmark->Opportunity): an alumnus is a real person whose record
    # should survive even if the opportunity row they're linked to is ever
    # deleted -- losing the link is acceptable, losing the person's record
    # is not.
    #
    # The matchmaker router queries this column filtered to an exact
    # `opportunity_id` -- it does NOT fuzzy-match against "similar"
    # opportunities (e.g. via embedding distance, the way
    # recommendations.py does for Opportunity-to-Opportunity similarity).
    # That's a disclosed simplification; see the README "Not included"
    # note on this.
    matched_opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("opportunities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    def __repr__(self) -> str:
        return f"<Alumnus id={self.id} full_name={self.full_name!r}>"
