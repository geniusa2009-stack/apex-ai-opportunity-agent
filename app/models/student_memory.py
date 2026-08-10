import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Dimension for Gemini's text-embedding-004, per this step's explicit ask.
# Deliberately a SEPARATE constant from app.models.opportunity.EMBEDDING_DIM
# (1536) -- that one sizes OpenAI's text-embedding-3-small, used for
# Opportunity-to-Opportunity dedup/search (app/services/ai.py,
# app/services/opportunity_search.py). This is a genuinely different
# provider, model, and dimensionality for a genuinely different purpose
# (private per-student semantic memory, not a public opportunity catalog)
# -- see app/services/memory_service.py for the Gemini embedding call
# itself.
MEMORY_EMBEDDING_DIM = 768


class StudentMemory(Base):
    """Layer 3 of the Enterprise Memory & Cloud Vault architecture: a
    semantic, per-artifact summary of what one saved UserHistory artifact
    proves about the student, plus its vector embedding -- enabling
    `semantic_search_memories` (app/services/memory_service.py) to find
    relevant context across a student's whole history for injection into
    future AI prompts, without re-reading every past artifact's full JSON
    payload out of Supabase Storage.

    No `created_at`/`TimestampMixin` -- same literal-field-list posture as
    UserHistory (see that model's docstring): the ask names exactly `id`,
    `user_id`, `artifact_id`, `memory_text`, `embedding` for this table.
    """

    __tablename__ = "student_memories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # CASCADE per the ask's explicit instruction -- deleting a UserHistory
    # artifact should delete its derived memory too; there's nothing left
    # for the memory to summarize once the artifact it's about is gone.
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_history.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    memory_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Not nullable -- unlike Opportunity.embedding (which supports an
    # "insert now, embed later" flow for freshly-scraped rows awaiting AI
    # verification), a StudentMemory row is only ever created by
    # app.services.memory_service.generate_and_store_embedding AFTER the
    # embedding has already been successfully computed -- there's no
    # partial/pending state for this table to represent.
    embedding: Mapped[list[float]] = mapped_column(Vector(MEMORY_EMBEDDING_DIM), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<StudentMemory id={self.id} user_id={self.user_id} "
            f"artifact_id={self.artifact_id}>"
        )
