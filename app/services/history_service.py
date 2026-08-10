import logging
import uuid
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_history import UserHistory
from app.services.storage_service import storage_service
from app.workers.memory_tasks import generate_memory_for_artifact

logger = logging.getLogger(__name__)


async def save_artifact(
    db: AsyncSession,
    user_id: UUID,
    feature_key: str,
    title: str,
    payload: dict,
    is_pinned: bool = False,
) -> UserHistory:
    """Saves one AI-generated artifact into the Enterprise Memory & Cloud
    Vault system: Postgres metadata (Layer 1) + the full payload in
    Supabase Storage (Layer 2) + a background Celery task to derive a
    semantic-memory embedding (Layer 3). This is exactly the 3-step flow
    `POST /history/` (app/api/v1/endpoints/history.py) already performs.

    Extracted into this standalone function for this step (Ecosystem
    Feature 3: AI Detector Safeguard & Essay Humanizer) -- that step's
    ask explicitly names a `save_artifact` helper ("save the results
    automatically as an artifact in the user_history table (using
    save_artifact...)") that didn't exist as an importable function yet.
    The original History step put this logic directly inline in its
    router handler, since at the time it was the only caller. This is a
    genuine refactor, not just new code alongside the old: `history.py`'s
    own `create_history_entry` now calls this same function instead of
    duplicating its body, so there's exactly one place this save flow is
    implemented, and any future feature that wants to save its own
    output as a history artifact (as `essay_humanizer.py` now does) can
    call this directly rather than re-deriving it.

    Deliberately does NOT catch `StorageUploadError` -- that decision is
    left to each caller. `history.py`'s router turns it into a 502 (the
    save IS the point of that endpoint); `essay_humanizer.py`'s router
    treats a save failure as non-fatal to a request whose primary value
    (the humanized essay) already succeeded before this is even called --
    see that router for the disclosed reasoning.
    """
    artifact_id = uuid.uuid4()

    storage_path = await storage_service.upload_json_payload(user_id, artifact_id, payload)

    history = UserHistory(
        id=artifact_id,
        user_id=user_id,
        feature_key=feature_key,
        title=title,
        storage_path=storage_path,
        is_pinned=is_pinned,
    )
    db.add(history)
    await db.commit()
    await db.refresh(history)

    # Fire-and-forget, same as the original inline implementation -- see
    # app/workers/memory_tasks.py's docstring for the full flow.
    generate_memory_for_artifact.delay(str(user_id), str(artifact_id), feature_key, title, payload)

    return history
