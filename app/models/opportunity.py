import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import AcademicLevel, Gender, OpportunityType
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.bookmark import UserBookmark
    from app.models.source import Source

# Dimension for the `embedding` column. 1536 matches OpenAI's
# text-embedding-3-small. If the embedding model changes later, this needs
# a migration (drop/recreate the column, or ALTER ... TYPE vector(N)) since
# pgvector column width is fixed at creation time.
EMBEDDING_DIM = 1536


class Opportunity(Base, TimestampMixin):
    """The core aggregated record: a scholarship, internship, volunteer
    role, or course pulled from a Source and (eventually) AI-verified.

    Hybrid Opportunity Aggregation Engine step additions/decisions:

    - `is_local_egypt`/`target_age_group` (new columns) -- genuinely new
      concepts this table had no prior field for; added exactly as asked.
    - `original_link` -> this codebase already has `target_url` (below),
      an identical concept (the canonical outbound application URL,
      already NOT NULL, already this table's existing dedup key -- see
      `app/scraper/pipelines.py`'s exact-URL de-dupe guard). Adding a
      SECOND column holding the same URL under a different name would
      mean every future writer has to remember to keep both in sync, and
      every future reader has to know which one is authoritative --
      `original_link` is treated as this ask's own name for `target_url`,
      not a new column.
    - `source_name`/`source_type` -> NOT added here. See `Source`'s own
      module docstring (app/models/source.py) for the full disclosed
      reasoning: both concepts already live there, one hop away via the
      existing `source` relationship, and duplicating them here would be
      a real normalization/data-integrity regression, not a convenience.
    - `country`'s default -> this column already existed (nullable,
      no default). This step's ask wants a `'Global'` default; migration
      0020 adds `server_default='Global'` to the column so every NEW row
      lacking an explicit country defaults to it, without backfilling
      existing NULL rows (a live product default vs. a historical-data
      rewrite are different decisions -- only the former was asked for).
    """

    __tablename__ = "opportunities"
    __table_args__ = (
        Index("ix_opportunities_type_deadline", "type", "deadline"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    type: Mapped[OpportunityType] = mapped_column(
        # `values_callable` -- disclosed fix, found while wiring scholarships.html
        # to this exact column (Step: scholarships.html <-> opportunities.py).
        # Without it, SQLAlchemy's default Enum bind-processor sends the Python
        # enum MEMBER NAME ("SCHOLARSHIP") to Postgres, not `.value`
        # ("scholarship") -- but migration 0001 created the real
        # `opportunity_type` Postgres enum type with the lowercase VALUES only
        # (`sa.Enum("scholarship", "internship", "volunteer", "course", ...)`).
        # Confirmed as a real crash, not a hypothesis: inserting a real
        # `Opportunity(type=OpportunityType.SCHOLARSHIP, ...)` -- the exact
        # pattern `app/services/aggregator_service.py`'s own
        # `upsert_opportunity` uses -- raised
        # `asyncpg.exceptions.InvalidTextRepresentationError: invalid input
        # value for enum opportunity_type: "SCHOLARSHIP"` before this fix.
        # This means the real opportunity-aggregation pipeline could never
        # have successfully inserted a single row.
        SAEnum(
            OpportunityType,
            name="opportunity_type",
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        index=True,
    )

    deadline: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    eligibility_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    funding_details: Mapped[str | None] = mapped_column(Text, nullable=True)

    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # `server_default="Global"` added by migration 0020 -- see class
    # docstring. Still nullable at the Python/ORM level (existing rows
    # predate the default and remain NULL, not backfilled).
    country: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    # --- Hybrid Opportunity Aggregation Engine additions (migration 0020) ---
    # See class docstring for the full disclosed reasoning on this
    # step's other three named fields (original_link/source_name/source_type).
    is_local_egypt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True
    )
    target_age_group: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # --- Backend Folder Structure & Core Database Models step addition ---
    # `ARRAY` of the new native `AcademicLevel` enum (see app/models/
    # enums.py's own docstring) -- this ask's own literal "Array of
    # Enums, linking to the User's academic level for strict filtering."
    # ADDITIVE alongside `target_age_group` above, not a replacement --
    # see `AcademicLevel`'s own docstring for the full disclosed
    # reasoning. `NULL`/empty (the common case for every pre-existing
    # row, and for any future scrape the aggregator's own extraction
    # prompt hasn't been taught to populate this field from) is treated
    # as "no academic-level restriction specified" by the dashboard's
    # matching query (`app/api/v1/endpoints/dashboards.py`) -- broadly
    # applicable, same "missing signal excludes nothing" posture already
    # established for `target_age_group.is_(None)`.
    target_academic_levels: Mapped[list[AcademicLevel] | None] = mapped_column(
        # Same disclosed `values_callable` fix as `type` above, and for the
        # identical reason (`AcademicLevel.PREP_1` has value `"prep_1"`, not
        # `.name` `"PREP_1"`) -- currently untriggered in practice (every row
        # has this column NULL until a future aggregator-prompt update
        # populates it, per this column's own comment below), but would hit
        # the exact same crash the moment anything ever wrote a real value.
        ARRAY(
            SAEnum(
                AcademicLevel,
                name="academic_level",
                native_enum=True,
                create_type=False,
                values_callable=lambda enum_cls: [e.value for e in enum_cls],
            )
        ),
        nullable=True,
    )

    # --- Real Age/Academic-Year/Gender Opportunity Filtering step ---
    # `min_age`/`max_age` -- genuinely new: this table previously had no
    # numeric age-range concept at all, only the coarse free-text
    # `target_age_group` bucket above (confirmed via a full-model read
    # before adding these). Plain nullable `Integer` columns -- matches
    # `User.age`'s own type exactly (Integer), the field these are
    # compared against in `opportunities.py`'s new eligibility filter --
    # deliberately NOT a native range type (Postgres `int4range`); two
    # plain nullable integers keep the "missing means unrestricted"
    # semantics trivial to express independently for each bound (a real
    # opportunity might have a minimum with no maximum, or vice versa),
    # which a single range column would make more awkward to express as
    # "no lower bound"/"no upper bound" via SQLAlchemy's ORM layer.
    # `None` on either column means that particular bound is unrestricted
    # -- the same "missing signal excludes nothing" posture already
    # established for `target_age_group.is_(None)`/`target_academic_levels.is_(None)`.
    min_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_age: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # `target_gender` -- see `Gender`'s own docstring (app/models/enums.py)
    # for why this is a single nullable value, not an array. `NULL` means
    # no gender restriction (open to everyone), matching every other
    # eligibility field's NULL convention on this model.
    #
    # DISCLOSED SCOPE BOUNDARY, same posture as `target_academic_levels`
    # above: NOT added to `OpportunityCreate`/`AIExtractedOpportunityFields`
    # in this pass -- `aggregator_service.py`'s own Gemini extraction
    # prompt has not been taught to extract a gender-restriction signal
    # from a scraped posting (confirmed via a direct read of that
    # prompt/schema before writing this column). Every row this schema
    # serializes will have `target_gender: null` (i.e. unrestricted, the
    # correct default for existing data) until either a future
    # aggregator-prompt update or manual admin curation populates it --
    # same real, honest limitation already disclosed for
    # `target_academic_levels`, not a new one this step introduces.
    target_gender: Mapped[Gender | None] = mapped_column(
        SAEnum(
            Gender,
            name="gender",
            native_enum=True,
            create_type=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )

    # --- AI verification pipeline fields (Phase 2 consumes/writes these) ---

    # Flipped to True once the AI verification worker confirms legitimacy
    # and the record is promoted out of the review queue.
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # 0.0-1.0 confidence score returned by the verification LLM call.
    ai_confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # The untouched, raw structured output from the scraper/LLM extraction
    # step, kept for auditability and re-processing if the extraction
    # schema changes later.
    raw_extracted_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Semantic embedding of title+description, used for dedup (cosine
    # distance against other Opportunity rows) and "similar opportunities"
    # search. No ANN index (ivfflat/hnsw) yet -- added in a later migration
    # once there's real data volume to tune it against.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)

    source: Mapped["Source"] = relationship(back_populates="opportunities", lazy="selectin")
    bookmarks: Mapped[list["UserBookmark"]] = relationship(
        back_populates="opportunity",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Opportunity id={self.id} title={self.title!r} type={self.type}>"
