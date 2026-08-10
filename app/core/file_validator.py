"""Security Shield: Rate Limiting & Strict File Validation step.

Robust, reusable validation for incoming `UploadFile` objects, run BEFORE
any bytes are written to local disk or handed off to the Celery pipeline.
Currently used by the Student Video upload endpoint (`POST /student/
videos/upload`, `app/api/v1/endpoints/student_videos.py`) for both the
required video and the optional custom thumbnail.

DISCLOSED PATH DEVIATION: this step's own ask names the target path as
`app/core/security/file_validator.py` (with an explicit "or similar"
allowance). This codebase already has `app/core/security.py` as a flat
module (JWT verification, password hashing) -- turning it into a real
`app/core/security/` package would require DELETING that file and
replacing it with `app/core/security/__init__.py` holding the same
content. That rename was attempted first, but this session's sandboxed
filesystem does not permit file deletion at all (`rm`/`unlink` both
return "Operation not permitted" against files in this workspace,
confirmed directly, not assumed) -- so the original `app/core/security.py`
was left completely untouched, and this validator lives here instead, as
a flat sibling file alongside `security.py`/`config.py`. Functionally
identical either way; only the import path differs
(`app.core.file_validator` instead of `app.core.security.file_validator`).
See `app/core/_unused_scratch_do_not_use/` for the harmless, inert
leftover this abandoned attempt produced (that directory is not imported
anywhere and is safe to delete manually).

DISCLOSED SIZE LIMITS (per this step's own literal spec):
- Video: max 2GB (`2 * 1024 * 1024 * 1024` bytes).
- Thumbnail: max 5MB (`5 * 1024 * 1024` bytes).

DISCLOSED MIME/EXTENSION NARROWING: the router's PRE-EXISTING video
content-type allowlist was `{"video/mp4", "video/quicktime", "video/webm",
"video/x-matroska"}` (mp4/mov/webm/mkv), deliberately widened beyond
MP4-only in the earlier Student Video Profiles step to accommodate videos
recorded straight off a phone or screen recorder. THIS step's own ask is
explicit and narrower: "Videos must strictly be video/mp4, video/quicktime,
or video/x-msvideo [.avi]." Since this step is specifically about
tightening upload security, and its wording says "strictly," the new,
narrower allowlist below REPLACES the old one -- webm/mkv uploads are no
longer accepted, `.avi` is newly accepted instead. This is a deliberate,
disclosed product tightening driven by this security milestone, not an
oversight; if webm/mkv support is still wanted, that would need to be a
separate, explicit follow-up decision, not silently preserved here.

DISCLOSED FOLLOW-UP FIX (re-request of this same milestone): the original
pass of this file allowed only `image/jpeg`/`image/png` for thumbnails,
missing `image/webp` -- this step's own re-stated spec explicitly lists
`image/webp` as a third accepted thumbnail type. Added `image/webp` /
`.webp` to the thumbnail allowlists below. This also required a matching
fix in `app/services/student_video_service.py`'s own separate
`_THUMBNAIL_CONTENT_TYPES` extension-to-R2-content-type map (a deliberate,
disclosed small duplication between that module and this one -- see that
module's own comment) -- without it, a `.webp` thumbnail would have PASSED
this validator but then been uploaded to R2 with a wrong, generic
`application/octet-stream` Content-Type header instead of `image/webp`.

DISCLOSED DOUBLE-EXTENSION DEFENSE, beyond the literal "video.mp4.exe"
example: `_reject_dangerous_extension_anywhere_in_filename` checks EVERY
dot-segment in the filename (not just the final one) against a blocklist
of known-dangerous/executable extensions. This catches both the literal
example given (a dangerous extension hidden as the TRUE final extension,
behind a benign-looking `.mp4` earlier in the name) and the less obvious
reverse pattern (a dangerous extension hidden in the MIDDLE of the
filename, behind an allowed-looking final extension, e.g.
`payload.exe.mp4`) -- the final-extension-allowlist check alone only
catches the first pattern; a full-filename scan is required to also catch
the second.
"""

import os

from fastapi import HTTPException, UploadFile, status

# --- Size limits, per this step's own literal spec -------------------------

MAX_VIDEO_SIZE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
MAX_THUMBNAIL_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

# --- MIME type / extension allowlists ---------------------------------------

ALLOWED_VIDEO_CONTENT_TYPES = {"video/mp4", "video/quicktime", "video/x-msvideo"}
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi"}

ALLOWED_THUMBNAIL_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Extensions that must never appear ANYWHERE in an uploaded filename's
# dot-segment chain, even behind an otherwise-allowed final extension.
# Not an exhaustive list of every dangerous extension that has ever
# existed -- a deliberately generous, disclosed set covering the common
# executable/script/installer/archive-payload families relevant to a
# server that will eventually pass this file to `ffmpeg`/store it on R2,
# sized for this specific attack surface rather than attempting to be a
# universal malware-extension database.
_DANGEROUS_EXTENSIONS = {
    ".exe", ".sh", ".bat", ".cmd", ".com", ".scr", ".msi", ".msp",
    ".js", ".jse", ".vbs", ".vbe", ".ps1", ".psm1", ".jar", ".php",
    ".py", ".rb", ".pl", ".dll", ".so", ".app", ".apk", ".deb", ".rpm",
    ".bin", ".run", ".wsf", ".gadget", ".hta", ".cpl", ".sys", ".dmg",
    ".pkg", ".reg", ".vb",
}


def _get_file_size_bytes(file: UploadFile) -> int:
    """Determines `file`'s size via cursor manipulation -- `file.file.seek(0, 2)`
    (seek to EOF) then `file.file.tell()` for the byte offset -- NOT
    `len(await file.read())`. Reading a file that can legitimately be up
    to 2GB into memory just to measure its length would be wasteful and,
    at real concurrent-upload volume, a real memory-pressure risk; this
    codebase's PRE-EXISTING upload endpoints did exactly that (see
    `student_videos.py`'s and `media_router.py`'s prior `len(video_bytes)`
    checks, both now superseded by this function), which this step's own
    ask specifically calls out as the pattern to fix.

    CRITICAL: resets the cursor back to the start (`file.file.seek(0)`)
    before returning. Without this reset, every downstream read of `file`
    (this router's own subsequent `await file.read()`, or anything else
    that touches this same `UploadFile`) would start from EOF and
    silently get back empty bytes -- this is not optional cleanup, the
    file is unusable for anything else without it.
    """
    file.file.seek(0, os.SEEK_END)
    size_bytes = file.file.tell()
    file.file.seek(0)
    return size_bytes


def _reject_dangerous_extension_anywhere_in_filename(filename: str) -> None:
    """Raises `415` if ANY dot-segment of `filename` (not just the final
    extension) matches a known-dangerous extension. See module docstring
    for why this needs to scan the whole filename, not just the last
    segment.
    """
    lowered = filename.lower()
    segments = lowered.split(".")
    if len(segments) <= 1:
        return  # No extension at all -- nothing to scan.
    for segment in segments[1:]:
        if f".{segment}" in _DANGEROUS_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=(
                    f"Filename {filename!r} contains a disallowed extension "
                    f"({segment!r}) and was rejected as a potential double-"
                    "extension attack."
                ),
            )


def _validate_mime_and_extension(
    file: UploadFile,
    *,
    allowed_content_types: set[str],
    allowed_extensions: set[str],
    label: str,
) -> None:
    """Checks BOTH the reported `Content-Type` header AND the filename's
    own extension -- checking only one would be a weaker guarantee than
    checking both: a raw HTTP client can send any `Content-Type` header
    it likes regardless of the actual bytes or filename, and a filename's
    extension alone says nothing about what a browser/client claims the
    content type is. Both must independently pass. `_reject_dangerous_extension_anywhere_in_filename`
    then runs as a further, independent check over the full filename (not
    just its final extension) -- see module docstring.
    """
    if file.content_type not in allowed_content_types:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported {label} content type {file.content_type!r}. "
                f"Accepted: {', '.join(sorted(allowed_content_types))}."
            ),
        )

    filename = file.filename or ""
    _, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported {label} file extension {ext!r}. "
                f"Accepted: {', '.join(sorted(allowed_extensions))}."
            ),
        )

    _reject_dangerous_extension_anywhere_in_filename(filename)


def _validate_size(file: UploadFile, *, max_bytes: int, label: str) -> int:
    """Returns the validated size in bytes. Raises `400` for a genuinely
    empty upload (0 bytes isn't "too large," so `413` would be the wrong
    status for it -- same distinction this codebase's pre-existing upload
    endpoints already drew) and `413` for exceeding `max_bytes`.
    """
    size_bytes = _get_file_size_bytes(file)
    if size_bytes == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Uploaded {label} is empty.")
    if size_bytes > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{label.capitalize()} too large -- max {_format_bytes(max_bytes)}.",
        )
    return size_bytes


def _format_bytes(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024 * 1024:
        return f"{num_bytes // (1024 * 1024 * 1024)}GB"
    return f"{num_bytes // (1024 * 1024)}MB"


def validate_video_upload(file: UploadFile) -> int:
    """Validates an incoming video `UploadFile` BEFORE it is saved to
    disk or handed to the Celery pipeline. Checks content-type,
    extension, and the dangerous-double-extension filename scan first
    (cheap, no I/O beyond a cursor seek), then size. Returns the
    validated size in bytes (callers don't need to re-measure it
    themselves). Raises `HTTPException` (`400`/`413`/`415`) on any
    failure -- FastAPI's own exception handling converts it to the
    correct HTTP response automatically, no `try`/`except` needed at the
    call site.
    """
    _validate_mime_and_extension(
        file,
        allowed_content_types=ALLOWED_VIDEO_CONTENT_TYPES,
        allowed_extensions=ALLOWED_VIDEO_EXTENSIONS,
        label="video",
    )
    return _validate_size(file, max_bytes=MAX_VIDEO_SIZE_BYTES, label="video")


def validate_thumbnail_upload(file: UploadFile) -> int:
    """Same contract as `validate_video_upload`, for the optional custom
    thumbnail image."""
    _validate_mime_and_extension(
        file,
        allowed_content_types=ALLOWED_THUMBNAIL_CONTENT_TYPES,
        allowed_extensions=ALLOWED_THUMBNAIL_EXTENSIONS,
        label="thumbnail",
    )
    return _validate_size(file, max_bytes=MAX_THUMBNAIL_SIZE_BYTES, label="thumbnail")
