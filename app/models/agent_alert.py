import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class AgentAlert(Base, TimestampMixin):
    """One run's worth of results from the Autonomous Scholar Agent
    (Step 3.27 / Legendary Feature 3) -- a background Celery task that
    searches the live web for opportunities matching a user's profile and
    drafts a personalized alert report.

    The ask offered three options for where to store this: a new
    `Notification`/`AgentAlert` model, or updating `User` to store just
    the latest alert. This went with a dedicated table (not "latest alert
    on User") specifically because `GET /scholar-agent/results` is meant
    to surface "the latest alerts/reports" (plural, per the ask) -- a
    single column on `User` would silently overwrite history on every
    run, losing everything but the most recent search.
    """

    __tablename__ = "agent_alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # CASCADE (not SET NULL, unlike Alumnus.matched_opportunity_id) --
    # this data is genuinely owned by the user, not a shared/independent
    # record like an alumnus. If the user is deleted, their alert history
    # should go with them.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    search_query: Mapped[str] = mapped_column(Text, nullable=False)
    report_text: Mapped[str] = mapped_column(Text, nullable=False)

    # List of match dicts, same shape as
    # `web_explorer.LiveOpportunityResult` (title/source_url/
    # brief_summary/deadline_hint) -- stored as plain JSONB rather than a
    # normalized child table since these are read-only, denormalized
    # snapshots of a point-in-time search, not rows that need their own
    # independent querying/updating.
    matches: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)

    def __repr__(self) -> str:
        return f"<AgentAlert id={self.id} user_id={self.user_id} matches={len(self.matches)}>"
