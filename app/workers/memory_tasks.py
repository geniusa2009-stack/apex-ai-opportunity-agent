import asyncio
import logging
import uuid

from app.core.celery_app import celery_app
from app.db.session import AsyncSessionLocal, engine
from app.services.memory_service import (
    ArtifactSummarizationError,
    MemoryEmbeddingError,
    generate_and_store_embedding,
    summarize_artifact_for_memory,
)

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.memory_tasks.generate_memory_for_artifact")
def generate_memory_for_artifact(
    user_id: str, artifact_id: str, feature_key: str, title: str, payload: dict
) -> dict:
    """Celery entrypoint (sync). Same `asyncio.run()` + `engine.dispose()`
    pattern as `app.workers.scholar_agent.run_autonomous_scholar_search` --
    see that task's docstring for the full event-loop-lifetime reasoning.

    Dispatched from `POST /history/` (app/api/v1/endpoints/history.py)
    right after the UserHistory row and Storage upload both succeed, so
    the request itself returns immediately without waiting on two extra
    Gemini round-trips (summarize, then embed) -- this is step 3 of the
    ask ("Run an async task to generate a semantic summary, call Gemini's
    embedding API, and store the vector"), read as a background job
    rather than something the request blocks on, consistent with every
    other Celery-task use in this codebase (app/workers/scholar_agent.py,
    app/workers/tasks.py).

    `user_id`/`artifact_id` are `str`, not `UUID` -- same reasoning as
    `run_autonomous_scholar_search`'s `user_id` param: Celery's configured
    `task_serializer="json"` (app/core/celery_app.py) can't serialize a
    raw `UUID`, so the caller passes `str(...)` and this task converts
    back with `uuid.UUID(...)` internally. `payload` is passed through
    as-is (a plain JSON-serializable dict), not re-fetched from Storage --
    the router already has it in memory from the request body, so handing
    it directly avoids a redundant Storage round-trip inside the task.
    """
    return asyncio.run(
        _generate_memory_for_artifact(user_id, artifact_id, feature_key, title, payload)
    )


async def _generate_memory_for_artifact(
    user_id: str, artifact_id: str, feature_key: str, title: str, payload: dict
) -> dict:
    try:
        try:
            memory_text = await summarize_artifact_for_memory(feature_key, title, payload)
        except ArtifactSummarizationError:
            logger.exception(
                "Memory summarization failed for artifact %s", artifact_id
            )
            return {"status": "summarization_failed", "artifact_id": artifact_id}

        async with AsyncSessionLocal() as session:
            try:
                memory = await generate_and_store_embedding(
                    session, uuid.UUID(user_id), uuid.UUID(artifact_id), memory_text
                )
            except MemoryEmbeddingError:
                logger.exception(
                    "Embedding generation failed for artifact %s", artifact_id
                )
                return {"status": "embedding_failed", "artifact_id": artifact_id}

            result = {
                "status": "completed",
                "artifact_id": artifact_id,
                "memory_id": str(memory.id),
            }
            logger.info("generate_memory_for_artifact: %s", result)
            return result
    finally:
        await engine.dispose()
