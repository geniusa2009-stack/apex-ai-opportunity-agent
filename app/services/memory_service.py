import logging
from uuid import UUID

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.models.student_memory import StudentMemory

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this
# codebase: `errors.ClientError` (429s) and `errors.ServerError` (5xx)
# both inherit from `errors.APIError`.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class ArtifactSummarizationError(Exception):
    """Raised when the Gemini call that condenses a raw artifact payload
    into a short semantic `memory_text` fails.

    This whole function (`summarize_artifact_for_memory`) is necessary
    plumbing not literally named in the ask: `generate_and_store_embedding`
    is specified as taking already-summarized `text_to_embed` as input,
    but the ask doesn't say what actually produces that summary text from
    a raw artifact JSON payload in the first place. Resolved the same way
    prior "the ask implies an input/step that isn't in the literal
    signature" gaps were resolved this session (see teacher_guide.py,
    culture_shock_evaluator.py) -- with a small, disclosed addition rather
    than leaving the pipeline unable to actually run end to end.
    """


class MemoryEmbeddingError(Exception):
    """Raised when Gemini's embedding call fails after retries."""


class MemorySemanticSearchError(Exception):
    """Raised when the semantic search pipeline -- embedding the query
    text, or the pgvector query itself -- fails. Same wrapping posture as
    app.services.opportunity_search.SemanticSearchError, applied here to
    StudentMemory instead of Opportunity."""


class _ArtifactMemorySummary(BaseModel):
    """Structured output for the summarization call. Single-field on
    purpose -- the only thing this call needs to produce is the text that
    `generate_and_store_embedding` will go on to embed."""

    memory_text: str = Field(
        description="A concise, semantic, third-person summary (1-3 "
        "sentences) of what this specific artifact proves about the "
        "student -- concrete skills, achievements, or experience "
        "demonstrated by its content, e.g. 'Student has strong Python "
        "skills and led a high school robotics club.' Not a restatement "
        "of the artifact's structure or feature name -- a summary of "
        "what a reader would learn about the student from it."
    )


_SUMMARIZATION_SYSTEM_PROMPT = """\
You are condensing one saved artifact from a student's history on Apex, a \
scholarship/internship/volunteer/course platform, into a short semantic \
memory: a concise note capturing what this artifact reveals about the \
student themselves, for later retrieval as context for other AI-assisted \
features on this platform (e.g. "does this student have leadership \
experience?").

You will be given which feature produced the artifact, its title, and its \
full JSON content.

Rules:
- Base the summary ONLY on what's actually present in the given content. \
Never invent a skill, achievement, or fact not evidenced by the artifact.
- Write in the third person, as a standalone factual note -- it will be \
read out of context later, with no access to the original artifact.
- Focus on what the artifact demonstrates about the student, not on \
describing the artifact's format or the feature that generated it.
- If the artifact's content is too sparse or generic to support any \
specific claim, write a brief, honest, generic summary rather than \
inventing specifics.
"""


def _client() -> genai.Client:
    # Same "fresh client per call" posture as every other Gemini-backed
    # service in this codebase -- see VisionGateway._client
    # (app/services/vision.py) for the full event-loop-lifetime reasoning.
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def summarize_artifact_for_memory(feature_key: str, title: str, payload: dict) -> str:
    """Condenses a raw artifact payload into the short semantic summary
    text that `generate_and_store_embedding` will embed."""
    prompt = f"Feature: {feature_key}\nTitle: {title}\nContent (JSON):\n{payload}"

    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_memory_summarizer_model,
            contents=[_SUMMARIZATION_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_ArtifactMemorySummary,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise ArtifactSummarizationError(str(exc)) from exc

    if not response.text:
        raise ArtifactSummarizationError(
            "Gemini returned no content for artifact summarization"
        )

    return _ArtifactMemorySummary.model_validate_json(response.text).memory_text


async def _embed_text(text: str, task_type: str) -> list[float]:
    """Shared embedding call for both storage-time (`RETRIEVAL_DOCUMENT`)
    and query-time (`RETRIEVAL_QUERY`) embeddings. text-embedding-004
    supports distinct task types tuned for each side of a retrieval
    pair -- using the matching one for each side is a documented
    accuracy recommendation for this model (not independently verified
    against a live API call in this sandbox, same disclosed limitation as
    every other Gemini call in this project; verify against
    https://ai.google.dev/gemini-api/docs/embeddings before relying on it
    staying accurate)."""
    response = await _client().aio.models.embed_content(
        model=settings.gemini_embedding_model,
        contents=[text],
        config=genai_types.EmbedContentConfig(task_type=task_type),
    )
    if not response.embeddings or response.embeddings[0].values is None:
        raise MemoryEmbeddingError("Gemini returned no embedding values")
    return response.embeddings[0].values


@_retry_gemini_call
async def generate_and_store_embedding(
    db: AsyncSession, user_id: UUID, artifact_id: UUID, text_to_embed: str
) -> StudentMemory:
    """Embeds `text_to_embed` via Gemini's `text-embedding-004` and
    persists it as a new StudentMemory row.

    Takes `db: AsyncSession` as an explicit first parameter -- not in the
    ask's literal signature (`generate_and_store_embedding(user_id,
    artifact_id, text_to_embed)`), but necessary plumbing: this function
    writes a row and needs a session to do it with. Matches the existing
    precedent of app.services.opportunity_search.semantic_search(db,
    query, ...), which takes `db` as its first parameter for the same
    reason -- services that touch the DB in this codebase accept an
    injected session rather than opening their own. Opening one's own
    session is reserved for the outermost Celery task entrypoint (see
    app/workers/memory_tasks.py, following app/workers/scholar_agent.py's
    established pattern).
    """
    try:
        vector = await _embed_text(text_to_embed, task_type="RETRIEVAL_DOCUMENT")
    except genai_errors.APIError:
        raise
    except MemoryEmbeddingError:
        raise
    except Exception as exc:
        raise MemoryEmbeddingError(str(exc)) from exc

    memory = StudentMemory(
        user_id=user_id,
        artifact_id=artifact_id,
        memory_text=text_to_embed,
        embedding=vector,
    )
    db.add(memory)
    await db.commit()
    await db.refresh(memory)
    return memory


async def semantic_search_memories(
    db: AsyncSession, user_id: UUID, query_text: str, limit: int = 5
) -> list[str]:
    """Cosine-similarity search over the calling user's OWN StudentMemory
    rows only (`user_id` filter) -- unlike
    opportunity_search.semantic_search, which searches globally across
    all verified Opportunity rows (a shared catalog), StudentMemory is
    private per-student context, so every query here is scoped to one
    user.

    Returns bare `memory_text` strings (per the ask's `list[str]` return
    type), meant to be injected directly into a future AI prompt as
    context -- not a richer object, since nothing downstream needs the
    embedding vector or artifact_id back out. Same `.cosine_distance()` /
    `order_by(distance.asc())` construction as
    app/services/opportunity_search.py's `semantic_search` -- see that
    function's comments for why pgvector's `<=>` operator measures
    distance, not similarity, and why there's no ANN index yet (same
    "deferred pending real data volume" posture as Opportunity.embedding).
    """
    try:
        query_vector = await _embed_text(query_text, task_type="RETRIEVAL_QUERY")
    except Exception as exc:
        logger.exception("Embedding generation failed for memory search query")
        raise MemorySemanticSearchError(str(exc)) from exc

    distance = StudentMemory.embedding.cosine_distance(query_vector)

    try:
        result = await db.execute(
            select(StudentMemory.memory_text)
            .where(StudentMemory.user_id == user_id)
            .order_by(distance.asc())
            .limit(limit)
        )
    except Exception as exc:
        logger.exception("pgvector query failed for memory search")
        raise MemorySemanticSearchError(str(exc)) from exc

    return list(result.scalars().all())
