import json
import logging
from uuid import UUID

import httpx
from supabase import acreate_client
from storage3.exceptions import StorageApiError

from app.core.config import settings

logger = logging.getLogger(__name__)

# Every artifact payload this service handles lives in this one bucket,
# per the ask's explicit instruction. The bucket itself is NOT created by
# this code -- Supabase Storage buckets are ordinarily provisioned once
# via the Supabase dashboard/CLI as part of project setup, the same way
# `database_url` already assumes a Supabase Postgres project exists
# rather than provisioning one. See the README's manual testing/setup
# notes for this step.
_BUCKET_NAME = "user-artifacts"

# Disclosed addition (Core Video Media Processing Pipeline step, built on
# top of Ecosystem Features 13 & 14): a second, separate bucket for raw
# (pre-transcode) lesson video uploads -- see `upload_raw_bytes`'s
# docstring below for why this handoff has to go through Storage at all
# rather than straight into the Celery task's arguments. Kept as its own
# bucket, not reused inside `user-artifacts`, since these are large,
# transient, binary video files with a completely different access
# pattern and lifecycle (written once by an admin upload, read exactly
# once by the transcode worker, safe to delete after -- unlike a
# student's permanent JSON artifact history).
_RAW_VIDEO_BUCKET_NAME = "raw-lesson-videos"


class StorageUploadError(Exception):
    """Raised when uploading a JSON payload to Supabase Storage fails."""


class StorageDownloadError(Exception):
    """Raised when downloading/parsing a JSON payload from Supabase
    Storage fails."""


class StorageDeleteError(Exception):
    """Raised when deleting a JSON payload from Supabase Storage fails."""


class StorageService:
    """Async wrapper around Supabase Storage (Layer 2 of the Enterprise
    Memory & Cloud Vault architecture) -- stores each saved artifact's
    full JSON payload as a `.json` object in the `user-artifacts` bucket,
    keyed off the artifact's own UUID, so Postgres (Layer 1, see
    app/models/user_history.py) only ever has to hold small, fast-to-scan
    metadata rows.

    Fresh async Supabase client per call -- the same posture (and the
    same reasoning) as every Gemini/OpenAI/Anthropic client already in
    this codebase (see AIGateway's docstring in app/services/ai.py, and
    VisionGateway._client in app/services/vision.py): this service is
    called both from FastAPI request handlers running inside one
    persistent event loop, and from a Celery task's own throwaway
    `asyncio.run()` loop (app/workers/memory_tasks.py isn't the caller
    here directly, but app/api/v1/endpoints/history.py's endpoints are
    themselves reachable from both contexts via `.delay()` chains
    elsewhere in this codebase). `supabase-py`'s async client wraps an
    `httpx.AsyncClient` bound to whichever loop constructed it -- reusing
    one across loops is exactly the same "Event loop is closed" failure
    mode already documented for the AI clients, so it isn't held as a
    long-lived attribute here either.

    Uses the service-role key (`settings.supabase_service_role_key`), not
    the anon/public key -- see the comment on that setting in
    app/core/config.py for why: this backend already writes to Postgres
    directly with its own credentials after verifying the caller itself
    (app/api/deps.py's `get_current_user`), rather than forwarding
    per-user tokens into Row Level Security. Storage access follows the
    same trust model.
    """

    async def _client(self):
        return await acreate_client(settings.supabase_url, settings.supabase_service_role_key)

    async def upload_json_payload(self, user_id: UUID, artifact_id: UUID, payload: dict) -> str:
        """Uploads `payload` as a `.json` object and returns the storage
        path it was written to (`history/{user_id}/{artifact_id}.json`,
        exactly the path shape the ask specifies)."""
        storage_path = f"history/{user_id}/{artifact_id}.json"
        client = await self._client()
        body = json.dumps(payload).encode("utf-8")

        try:
            await client.storage.from_(_BUCKET_NAME).upload(
                storage_path,
                body,
                file_options={"content-type": "application/json", "upsert": "true"},
            )
        except (StorageApiError, httpx.HTTPError) as exc:
            logger.exception("Supabase Storage upload failed for %s", storage_path)
            raise StorageUploadError(str(exc)) from exc

        return storage_path

    async def download_json_payload(self, storage_path: str) -> dict:
        """Fetches the `.json` object at `storage_path` and returns it
        parsed as a dict."""
        client = await self._client()

        try:
            raw_bytes = await client.storage.from_(_BUCKET_NAME).download(storage_path)
        except (StorageApiError, httpx.HTTPError) as exc:
            logger.exception("Supabase Storage download failed for %s", storage_path)
            raise StorageDownloadError(str(exc)) from exc

        try:
            return json.loads(raw_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.exception("Stored artifact at %s was not valid JSON", storage_path)
            raise StorageDownloadError(str(exc)) from exc

    async def delete_json_payload(self, storage_path: str) -> bool:
        """Deletes the `.json` object at `storage_path`. Returns True on
        success (Supabase Storage's remove endpoint doesn't error on an
        already-missing path, so this is idempotent -- calling it twice,
        or on a path that was never uploaded, isn't a failure)."""
        client = await self._client()

        try:
            await client.storage.from_(_BUCKET_NAME).remove([storage_path])
        except (StorageApiError, httpx.HTTPError) as exc:
            logger.exception("Supabase Storage delete failed for %s", storage_path)
            raise StorageDeleteError(str(exc)) from exc

        return True

    # -------------------------------------------------------------
    # Raw video handoff (Core Video Media Processing Pipeline step)
    # -------------------------------------------------------------
    #
    # Generic byte-oriented methods, distinct from the JSON-specific ones
    # above -- necessary plumbing, disclosed: the admin video-upload
    # endpoint (`media_router.py`) and the Celery transcode worker
    # (`app/workers/media_processing.py`) run in different processes
    # (potentially different machines entirely), so a raw video's bytes
    # can't just be passed as a Celery task argument -- Celery's
    # configured `task_serializer="json"` (app/core/celery_app.py) can't
    # carry multi-hundred-megabyte binary payloads through Redis
    # efficiently or safely even if it technically could serialize them.
    # The established, correct pattern for this handoff: the router
    # uploads the raw file to Storage and passes the Celery task only a
    # small string (the storage path); the worker downloads the actual
    # bytes itself once it picks up the job.

    async def upload_raw_bytes(self, storage_path: str, data: bytes, content_type: str) -> str:
        """Uploads arbitrary bytes to the raw-video bucket at
        `storage_path` and returns that same path (mirrors
        `upload_json_payload`'s "return the path you gave it" shape)."""
        client = await self._client()

        try:
            await client.storage.from_(_RAW_VIDEO_BUCKET_NAME).upload(
                storage_path,
                data,
                file_options={"content-type": content_type, "upsert": "true"},
            )
        except (StorageApiError, httpx.HTTPError) as exc:
            logger.exception("Supabase Storage raw upload failed for %s", storage_path)
            raise StorageUploadError(str(exc)) from exc

        return storage_path

    async def download_raw_bytes(self, storage_path: str) -> bytes:
        """Fetches the raw bytes at `storage_path` from the raw-video
        bucket."""
        client = await self._client()

        try:
            return await client.storage.from_(_RAW_VIDEO_BUCKET_NAME).download(storage_path)
        except (StorageApiError, httpx.HTTPError) as exc:
            logger.exception("Supabase Storage raw download failed for %s", storage_path)
            raise StorageDownloadError(str(exc)) from exc

    async def delete_raw_bytes(self, storage_path: str) -> bool:
        """Deletes the raw upload at `storage_path` -- called once the
        transcode worker has successfully downloaded it, so the (large,
        now-redundant -- the HLS output is what actually gets served
        going forward) original file doesn't sit in Storage forever.
        Same idempotent-on-missing-path posture as `delete_json_payload`."""
        client = await self._client()

        try:
            await client.storage.from_(_RAW_VIDEO_BUCKET_NAME).remove([storage_path])
        except (StorageApiError, httpx.HTTPError) as exc:
            logger.exception("Supabase Storage raw delete failed for %s", storage_path)
            raise StorageDeleteError(str(exc)) from exc

        return True


# Module-level singleton -- stateless (each call builds its own client),
# so sharing this instance across tasks/requests is safe. Same pattern as
# `ai_gateway` (app/services/ai.py) and `vision_gateway`
# (app/services/vision.py).
storage_service = StorageService()
