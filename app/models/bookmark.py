import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import BookmarkStatus

if TYPE_CHECKING:
    from app.models.courses import Course
    from app.models.opportunity import Opportunity
    from app.models.user import User


class UserBookmark(Base):
    """A user's saved/applied relationship to either an Opportunity OR a
    Course.

    --- Hyper-Personalized Platform step: polymorphic bookmark extension ---
    That step's own literal ask: "Many-to-Many associative layout linking
    `user_id` to `course_id` OR `opportunity_id`." Prior to this step,
    `opportunity_id` was the only target and was NOT NULL -- every existing
    row (and every existing test/router call site, see `bookmarks.py`) has
    a real opportunity, none has a course. Resolved as a genuine, disclosed
    polymorphic-FK design (not a rename, not a second parallel table):

    1. `opportunity_id` becomes NULLABLE (was NOT NULL).
    2. A new, equally nullable `course_id` FK to `courses.id` is added,
       CASCADE on delete, matching every other FK in this table.
    3. `CheckConstraint` `ck_bookmark_exactly_one_target` enforces that
       EXACTLY ONE of the two is set on any row -- neither both (which
       would make `status`/"what is this a bookmark of" ambiguous) nor
       neither (a bookmark that points at nothing).
    4. The single `UniqueConstraint` on `(user_id, opportunity_id)` is kept
       (renamed target unchanged) and a SECOND, parallel one is added for
       `(user_id, course_id)`. Postgres (and SQLite) treat NULL as
       distinct from any other NULL for uniqueness purposes, so a user's
       many course-bookmark rows (all with `opportunity_id IS NULL`) never
       collide with each other under the opportunity constraint, and vice
       versa -- both constraints coexist safely on every row regardless of
       which target it points at.
    5. `opportunity`/`course` are now both `Mapped[... | None]`
       relationships -- exactly one populated per row, mirroring the FKs.
       `Course.bookmarks` (new, symmetric `back_populates`) is added on
       `Course` itself (`app/models/courses.py`) alongside its existing
       relationships.

    Disclosed scope boundary: the existing `POST /bookmarks/{opportunity_id}`
    router endpoint (`app/api/v1/endpoints/bookmarks.py`) is UNCHANGED by
    this step -- it still only ever creates opportunity-typed bookmarks.
    No course-bookmarking endpoint exists yet; extending that router (or
    adding a parallel `POST /bookmarks/courses/{course_id}`) was not named
    in this step's own ask (which asked for the "Model" only, under
    "Advanced Integrated Schemas"), so it is left for a future pass rather
    than speculatively invented here.
    """

    __tablename__ = "user_bookmarks"
    __table_args__ = (
        UniqueConstraint("user_id", "opportunity_id", name="uq_user_opportunity_bookmark"),
        UniqueConstraint("user_id", "course_id", name="uq_user_course_bookmark"),
        CheckConstraint(
            "(course_id IS NOT NULL AND opportunity_id IS NULL) "
            "OR (course_id IS NULL AND opportunity_id IS NOT NULL)",
            name="ck_bookmark_exactly_one_target",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # New in the Hyper-Personalized Platform step -- see class docstring.
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    status: Mapped[BookmarkStatus] = mapped_column(
        # APEX User Intelligence + Proactive Guidance step -- DISCLOSED
        # PRE-EXISTING BUG FIX, found while writing this phase's own
        # tests (`tests/test_user_intelligence.py`'s SNAPSHOT rows are
        # the first tests in this repository to actually round-trip a
        # populated `UserBookmark.status` through the ORM -- the prior
        # phase's own fixture created this table but never inserted a
        # row into it). Without `values_callable`, SQLAlchemy's `Enum`
        # type maps a Python enum member by its NAME ("SAVED"/"APPLIED")
        # by default, but migration 0001 created the real Postgres
        # `bookmark_status` enum TYPE with the lowercase VALUE labels
        # ("saved"/"applied") -- exactly matching `BookmarkStatus.SAVED
        # .value`, not `.name`. Every other native-enum column in this
        # codebase (`User.academic_level`/`Opportunity.type`/etc.) already
        # carries this same `values_callable` fix; `UserBookmark.status`
        # was the one column that never got it, which would make every
        # real Postgres bookmark write raise "invalid input value for
        # enum bookmark_status: 'SAVED'". No migration needed -- the
        # Postgres enum TYPE's labels are already correct; only this
        # Python-side mapping was wrong.
        SAEnum(
            BookmarkStatus, name="bookmark_status", native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        default=BookmarkStatus.SAVED,
        nullable=False,
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="bookmarks", lazy="selectin")
    opportunity: Mapped["Opportunity | None"] = relationship(
        back_populates="bookmarks", lazy="selectin"
    )
    course: Mapped["Course | None"] = relationship(back_populates="bookmarks", lazy="selectin")

    def __repr__(self) -> str:
        return (
            f"<UserBookmark user_id={self.user_id} "
            f"opportunity_id={self.opportunity_id} course_id={self.course_id} "
            f"status={self.status}>"
        )
