"""Cloud Garbage Collection (IPFS Unpinning & R2 Deletion).

`StorageCleanupService` -- a unified, BEST-EFFORT deletion layer over
this codebase's two remote media backends: Cloudflare R2 (Student Video
HLS manifests/segments + thumbnails, since the Student Video Pinata -> R2
Migration step) and Pinata/IPFS (Course/Lesson HLS manifests/thumbnails,
which never migrated off Pinata -- see `app/core/config.py`'s R2 settings
block for that disclosed, deliberate scope boundary).

CORE DESIGN CONTRACT, disclosed up front because it shapes every method
below: **neither method here ever raises.** This module's only real
caller is a Celery background task (`app/workers/cleanup_tasks.py`)
dispatched fire-and-forget from an HTTP request handler that has ALREADY
told the user their delete succeeded (the DB row is gone) by the time
this code runs. A storage provider being briefly unreachable, a key
already having been cleaned up by an earlier attempt, or a malformed
stored URL must never surface as an unhandled exception that kills a
Celery worker or gets silently swallowed by Celery's own default
error-logging with no context -- every failure path here is caught,
logged with enough detail to actually investigate it, and reported back
to the caller as a plain `bool` (`True` = deleted/unpinned or "already
gone, nothing to do"; `False` = a real failure worth knowing about).

Deliberately synchronous, top to bottom -- same reasoning as
`r2_service.py`/`media_service.py`'s own Pinata functions: this module's
only caller runs inside a Celery task, which already has its own
worker thread/process with no other concurrent coroutines competing for
it. `boto3` has no native asyncio support anyway, and `httpx.Client`
(not `AsyncClient`) matches the synchronous Pinata call pattern already
established in `media_service.py`.

A fresh `boto3` client is built per call (`_r2_client`, mirroring
`r2_service.py`'s own `_r2_client` factory verbatim -- same client
construction, same "no shared client cached across Celery task
invocations" posture, deliberately duplicated here rather than imported:
`r2_service.py`'s own module docstring frames it as owning UPLOADS
specifically; adding an unrelated DELETE code path into that module would
blur its own stated boundary the same way adding Pinata pinning to
`media_service.py` would have. This module owns cleanup/deletion across
BOTH backends, the same way `r2_service.py` owns R2 uploads and
`media_service.py` owns Pinata uploads + ffmpeg).
"""

import logging

import boto3
import httpx
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)

# Same literal value as `r2_service.R2_UPLOAD_TIMEOUT_SECONDS` for the
# boto3 client's own connect/read timeouts -- deletion is a much cheaper
# operation than an upload, but reusing the same constant keeps this
# module's client construction visibly consistent with `r2_service.py`'s,
# rather than introducing a second, arbitrarily-different timeout value
# for no functional reason.
_R2_REQUEST_TIMEOUT_SECONDS = 300

# Generous but bounded -- an unpin call is a single small HTTP request,
# not a large upload, so this is deliberately much shorter than
# `media_service.PINATA_UPLOAD_TIMEOUT_SECONDS` (300s); a genuinely stuck
# connection here should fail fast rather than tie up a Celery worker
# slot for five minutes over what is, worst case, a no-op cleanup.
_PINATA_UNPIN_TIMEOUT_SECONDS = 30

_PINATA_UNPIN_URL_TEMPLATE = "https://api.pinata.cloud/pinning/unpin/{cid}"


def _r2_client():
    """Builds a fresh `boto3` S3-compatible client pointed at this
    account's R2 endpoint -- byte-for-byte the same construction as
    `r2_service._r2_client`. See that function's own docstring for why a
    fresh, non-cached client is deliberate here, and why `region_name=
    "auto"` / the account-scoped `endpoint_url` are what they are.
    """
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=BotoConfig(
            connect_timeout=_R2_REQUEST_TIMEOUT_SECONDS,
            read_timeout=_R2_REQUEST_TIMEOUT_SECONDS,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _extract_r2_key(object_url: str) -> str | None:
    """Reverses `r2_service._public_url` -- turns a full public URL
    (`{r2_public_base_url}/{key}`) back into the bare object `key`
    `boto3`'s `delete_object` needs. Returns `None` (rather than raising
    or guessing) if `object_url` doesn't actually start with this
    account's own configured `r2_public_base_url`: a stored URL that
    doesn't match this deployment's own bucket is either a data-integrity
    bug worth logging loudly, or -- more mundanely -- this environment's
    `r2_public_base_url` was reconfigured (a custom domain change, a
    bucket migration) AFTER some rows were already written under the OLD
    base URL. Either way, blindly stripping some OTHER prefix and
    deleting whatever key falls out the other end risks deleting the
    wrong object entirely -- returning `None` here and letting the caller
    log-and-skip is the safe failure mode.
    """
    if not settings.r2_public_base_url:
        return None
    base = settings.r2_public_base_url.rstrip("/") + "/"
    if not object_url.startswith(base):
        return None
    key = object_url[len(base) :]
    return key or None


class StorageCleanupService:
    """Unified best-effort deletion across Cloudflare R2 and Pinata/IPFS.
    See module docstring for the "never raises" contract both methods
    share.
    """

    def delete_from_r2(self, object_url: str) -> bool:
        """Deletes the R2 object addressed by `object_url` (a full public
        URL, e.g. `StudentVideo.hls_master_url`/`.thumbnail_url`).

        Returns `True` if the object was deleted (or -- see below -- was
        already gone), `False` on any real failure. Never raises.

        DISCLOSED S3/R2 SEMANTIC NUANCE: unlike, say, a filesystem
        `os.remove`, S3-compatible `DeleteObject` (which R2 implements)
        does NOT raise a "not found" error for a key that doesn't exist
        -- it returns a plain success (`204 No Content`) either way,
        treating delete as idempotent by design. This means the "gracefully
        handle 404s" requirement this step's own ask names is, in
        practice, already satisfied by `boto3`/R2's own API contract for
        the overwhelmingly common case (a key genuinely already gone).
        The `except (ClientError, BotoCoreError)` branch below exists for
        the OTHER real failure modes this call can still hit: R2
        unreachable, wrong/expired credentials, a bucket-level permission
        error, or (rarely, and provider-dependent) some S3-compatible
        backend that DOES surface a `NoSuchKey`/404-shaped `ClientError`
        for certain configurations -- all handled identically here (log,
        return `False`), since none of them should ever crash the Celery
        task that called this.
        """
        if not settings.r2_account_id or not settings.r2_bucket_name:
            logger.warning(
                "Cannot delete R2 object %r -- Cloudflare R2 is not configured (missing account id or bucket name).",
                object_url,
            )
            return False

        key = _extract_r2_key(object_url)
        if key is None:
            logger.warning(
                "Cannot delete R2 object -- %r does not match this deployment's own r2_public_base_url; skipping "
                "rather than risk deleting the wrong object.",
                object_url,
            )
            return False

        try:
            client = _r2_client()
            client.delete_object(Bucket=settings.r2_bucket_name, Key=key)
        except (ClientError, BotoCoreError) as exc:
            error_code = _client_error_code(exc)
            if error_code in {"NoSuchKey", "404"}:
                logger.info("R2 object %r (key %r) was already gone -- treating as success.", object_url, key)
                return True
            logger.error("Failed to delete R2 object %r (key %r): %s", object_url, key, exc)
            return False

        logger.info("Deleted R2 object %r (key %r).", object_url, key)
        return True

    def unpin_from_pinata(self, cid: str) -> bool:
        """Unpins `cid` from this account's Pinata pin set via `DELETE
        /pinning/unpin/{cid}`.

        Returns `True` if the CID was unpinned (or -- see below -- was
        already not pinned), `False` on any real failure. Never raises.

        Pinata's own documented behavior for this endpoint returns a
        `404` (with a JSON error body, typically something like `"CID not
        found"` / `"PIN_NOT_FOUND"`) when the CID isn't currently pinned
        under this account -- exactly the "gracefully handle 404s" case
        this step's own ask names explicitly. Treated as a successful
        no-op here (nothing left to unpin is the same end state as
        successfully unpinning it), not a failure.
        """
        if not settings.pinata_jwt:
            logger.warning("Cannot unpin CID %r -- PINATA_JWT is not configured.", cid)
            return False

        url = _PINATA_UNPIN_URL_TEMPLATE.format(cid=cid)

        try:
            with httpx.Client(timeout=_PINATA_UNPIN_TIMEOUT_SECONDS) as client:
                response = client.delete(url, headers={"Authorization": f"Bearer {settings.pinata_jwt}"})
        except httpx.TimeoutException:
            logger.error("Timed out unpinning CID %r from Pinata after %ss.", cid, _PINATA_UNPIN_TIMEOUT_SECONDS)
            return False
        except httpx.HTTPError as exc:
            logger.error("Failed to reach Pinata to unpin CID %r: %s", cid, exc)
            return False

        if response.status_code == 404:
            logger.info("CID %r was already unpinned (or never pinned) -- treating as success.", cid)
            return True

        if response.is_success:
            logger.info("Unpinned CID %r from Pinata.", cid)
            return True

        logger.error("Pinata refused to unpin CID %r: %s %s", cid, response.status_code, response.text)
        return False


def _client_error_code(exc: Exception) -> str | None:
    """Best-effort extraction of a `botocore` `ClientError`'s own error
    code (e.g. `"NoSuchKey"`) -- returns `None` for a plain
    `BotoCoreError` (a connection-level failure with no such structured
    error response to inspect at all) rather than raising on the
    `.response` attribute access every `ClientError` has but
    `BotoCoreError` does not.
    """
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code")
    return None


# Module-level singleton -- stateless (every method builds its own
# fresh client/connection per call, per this module's own docstring), so
# a single shared instance is safe and avoids every caller constructing
# its own `StorageCleanupService()` for no benefit. Same posture as
# `app/services/chat_connection_manager.py`'s own `manager = ConnectionManager()`.
storage_cleanup_service = StorageCleanupService()
