"""Student Video Pinata -> Cloudflare R2 Migration.

Cloudflare R2 replacement for the two Pinata upload functions this
pipeline used to call (`media_service.upload_hls_directory_to_pinata`/
`upload_file_to_pinata`) -- SAME two call shapes (upload a whole local
HLS output directory; upload one local file), same synchronous,
Celery-worker-only calling convention (see this module's own note on
that below), but writing to Cloudflare R2 via `boto3`'s S3-compatible
client instead of Pinata's `pinFileToIPFS` HTTP API.

Deliberately a NEW, separate module rather than added directly into
`media_service.py`: that module's own docstring is explicit that it is
"this codebase's one place for raw ffmpeg/ffprobe subprocess invocation"
plus Pinata pinning for the Core Video Media Processing Pipeline
(Course/Lesson videos, which this migration's own disclosed scope
explicitly does NOT touch -- see `app/core/config.py`'s R2 settings
block). Mixing a second, unrelated storage backend into that file would
blur its own stated boundary; this module owns R2 specifically, the same
way `storage_service.py` owns Supabase Storage.

Deliberately synchronous, top to bottom -- NOT `async def`, same
reasoning as `media_service.py`'s own Pinata functions: this module's
only real caller is `student_video_service.py`'s `transcode_and_upload_to_r2`/
`resolve_thumbnail_url`, themselves only ever invoked from inside
`app/tasks/student_video.py`'s Celery task, which already runs on its
own worker thread with no other concurrent coroutines competing for it.
`boto3`'s S3 client is itself a synchronous, blocking client (no asyncio
support without a separate wrapper library like `aioboto3`, which this
codebase deliberately does NOT add -- see the disclosed reasoning in the
README for why a second async S3 SDK dependency isn't worth it for a
Celery-only call site).
"""

import logging
import os

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)


class R2ConfigurationError(Exception):
    """Raised when R2 credentials/bucket/public URL are not fully
    configured -- distinct from `R2UploadError` (a configured client that
    still failed) so callers/logs can tell "not set up yet" apart from
    "set up, but this specific call failed"."""


class R2UploadError(Exception):
    """Raised when an upload to Cloudflare R2 fails."""


# Generous but bounded, same posture (and same literal value) as
# `media_service.PINATA_UPLOAD_TIMEOUT_SECONDS` -- an HLS output
# directory for a lecture-length video can be many megabytes across
# dozens of segments; this exists so a genuinely stuck/hung connection
# doesn't tie up a Celery worker slot forever.
R2_UPLOAD_TIMEOUT_SECONDS = 300

_CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
}


def _require_r2_configured() -> None:
    missing = [
        name
        for name, value in (
            ("r2_account_id", settings.r2_account_id),
            ("r2_access_key_id", settings.r2_access_key_id),
            ("r2_secret_access_key", settings.r2_secret_access_key),
            ("r2_bucket_name", settings.r2_bucket_name),
            ("r2_public_base_url", settings.r2_public_base_url),
        )
        if not value
    ]
    if missing:
        raise R2ConfigurationError(
            f"Cloudflare R2 is not fully configured -- missing: {', '.join(missing)}."
        )


def _r2_client():
    """Builds a fresh `boto3` S3-compatible client pointed at this
    account's R2 endpoint. Fresh client per call (not a module-level
    singleton) -- same "no shared client held across Celery task
    invocations" posture as every other external-API client in this
    codebase (see `student_video_service.py`'s own `_openai_client`/
    `_gemini_client` for the fuller version of that reasoning, which
    applies here for the same underlying "don't assume anything about
    the caller's process/thread lifecycle" reason, even though `boto3`
    itself has no asyncio-event-loop-binding issue the way an async SDK
    client would).

    `region_name="auto"` -- R2's own documented requirement for its
    S3-compatible endpoint; R2 has no real AWS-style regions, but
    `boto3`'s `Session.client()` requires SOME region value to be set,
    and Cloudflare's own docs specify the literal string `"auto"` here.

    `endpoint_url` is R2's per-account S3-compatible API endpoint --
    DIFFERENT from `settings.r2_public_base_url` (this bucket's PUBLIC
    read URL); see `settings.r2_public_base_url`'s own comment in
    app/core/config.py for why those are two distinct hostnames in R2's
    architecture, not the same value used two ways.
    """
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=BotoConfig(
            connect_timeout=R2_UPLOAD_TIMEOUT_SECONDS,
            read_timeout=R2_UPLOAD_TIMEOUT_SECONDS,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _public_url(key: str) -> str:
    base = settings.r2_public_base_url.rstrip("/")
    return f"{base}/{key.lstrip('/')}"


def upload_hls_directory_to_r2(directory: str, key_prefix: str) -> str:
    """Walks `directory` (the local HLS output tree `media_service.
    transcode_to_hls` just produced) and uploads every file to R2 under
    `key_prefix/` (e.g. `student-videos/{video_id}/hls/master.m3u8`,
    `student-videos/{video_id}/hls/720p/segment_000.ts`), preserving the
    exact relative directory structure `transcode_to_hls` produced --
    same reason `upload_hls_directory_to_pinata` preserves it: HLS
    playlists reference their own segment files via RELATIVE paths (e.g.
    `720p/stream.m3u8` referencing `segment_000.ts` in the same
    directory), so the uploaded object layout must mirror the local one
    exactly for the resulting `master.m3u8` to actually resolve its own
    variant playlists and segments once served back over HTTP.

    Returns the full, directly playable public URL to `master.m3u8` --
    DELIBERATELY the complete URL, not a bare key/prefix. Disclosed
    design choice, the central simplification this whole migration
    enables: unlike an IPFS CID (which every CONSUMER has to combine with
    a chosen gateway's own base URL, exactly what `lib/ipfsGateways.ts`'s
    `IPFS_GATEWAYS` fallback list exists to handle), an R2 object has
    exactly ONE real public URL. There is no "gateway" concept to defer
    to the caller here -- returning the fully-resolved URL means neither
    `StudentVideo.hls_master_url` nor any frontend consumer of it ever
    needs to know R2's bucket/key layout at all, only "here is a URL,
    play it."

    Raises `R2ConfigurationError` if R2 isn't configured, or
    `R2UploadError` on an empty directory or any `boto3`/R2 failure.
    """
    _require_r2_configured()

    file_specs: list[tuple[str, str, str]] = []
    for root, _dirs, filenames in os.walk(directory):
        for filename in sorted(filenames):
            abs_path = os.path.join(root, filename)
            relative_path = os.path.relpath(abs_path, directory).replace(os.sep, "/")
            content_type = _CONTENT_TYPES.get(os.path.splitext(filename)[1], "application/octet-stream")
            file_specs.append((relative_path, abs_path, content_type))

    if not file_specs:
        raise R2UploadError(f"No files found in {directory!r} to upload.")

    normalized_prefix = key_prefix.strip("/")
    client = _r2_client()

    logger.info("Uploading %d files (%s) to R2 under %r", len(file_specs), directory, normalized_prefix)

    try:
        for relative_path, abs_path, content_type in file_specs:
            key = f"{normalized_prefix}/{relative_path}"
            client.upload_file(
                abs_path,
                settings.r2_bucket_name,
                key,
                ExtraArgs={"ContentType": content_type},
            )
    except (ClientError, BotoCoreError) as exc:
        raise R2UploadError(f"R2 directory upload failed: {exc}") from exc

    return _public_url(f"{normalized_prefix}/master.m3u8")


def upload_file_to_r2(file_path: str, key: str, content_type: str) -> str:
    """Uploads a SINGLE local file (a student video thumbnail -- either a
    viewer-supplied custom image or an FFmpeg-extracted fallback frame,
    same two callers as `media_service.upload_file_to_pinata` before it)
    to R2 at the exact given `key`, returning its full public URL.

    `key` is passed in by the caller (`student_video_service.
    resolve_thumbnail_url`) fully formed (e.g.
    `student-videos/{video_id}/thumbnail.jpg`) rather than derived here --
    this function has no opinion on this codebase's own key-naming
    convention, matching `upload_hls_directory_to_r2`'s own posture of
    taking `key_prefix` as a caller-supplied string rather than
    constructing it internally.

    Raises `R2ConfigurationError` if R2 isn't configured, or
    `R2UploadError` if the local file is missing or any `boto3`/R2 call
    fails.
    """
    _require_r2_configured()

    if not os.path.isfile(file_path):
        raise R2UploadError(f"No file found at {file_path!r} to upload.")

    client = _r2_client()
    normalized_key = key.strip("/")

    logger.info("Uploading 1 file (%s) to R2 at %r", file_path, normalized_key)

    try:
        client.upload_file(
            file_path,
            settings.r2_bucket_name,
            normalized_key,
            ExtraArgs={"ContentType": content_type},
        )
    except (ClientError, BotoCoreError) as exc:
        raise R2UploadError(f"R2 file upload failed: {exc}") from exc

    return _public_url(normalized_key)
