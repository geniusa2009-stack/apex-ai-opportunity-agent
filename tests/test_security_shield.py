"""Behavioral tests for the Security Shield: Rate Limiting & Strict File
Validation step -- `app/core/file_validator.py` and
`app/api/dependencies/rate_limiter.py`, both wired into `POST /student/
videos/upload` (`app/api/v1/endpoints/student_videos.py`).

Per this step's own explicit verification requirement, three outcomes are
asserted: `413 Payload Too Large` on an oversized file, `415 Unsupported
Media Type` on an invalid MIME type/extension, and `429 Too Many Requests`
after 3 uploads already exist for the same user within a rolling 24-hour
window.

TEST DESIGN, disclosed: split across two classes.

`TestFileValidatorUnit` exercises `validate_video_upload`/
`validate_thumbnail_upload` DIRECTLY, not through a real HTTP request.
This is a deliberate choice for the SIZE-limit tests specifically: the
video limit is 2GB, and constructing a real 2GB+1-byte multipart body
(even just in-process, let alone over `TestClient`'s own request
machinery) in a test suite that runs on every commit would be genuinely
wasteful -- slow, memory-heavy, and testing the exact same validation
LOGIC a much cheaper synthetic file object can exercise just as
correctly. `_FakeCursorFile` below is a minimal seek/tell-only stand-in
that reports a given size WITHOUT allocating that many real bytes,
specifically to make the 2GB boundary case affordable to test at all --
every other test in this file uses REAL bytes (`io.BytesIO`).

`TestUploadRateLimitIntegration` exercises the rate limiter through the
REAL router endpoint via `TestClient` (same `get_current_user`/`get_db`
dependency-override technique as `tests/test_student_videos_my_uploads.py`),
since the whole point of that check is verifying the real DB query +
dependency wiring, not just the standalone function in isolation. The
Celery dispatch (`process_student_video_pipeline_task.delay`) is mocked
for every successful (202) case -- no real Celery broker is started for
these tests, consistent with this codebase's established testing posture
(see `tests/test_student_videos.py`'s own docstring).
"""

import io
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.dependencies.rate_limiter import MAX_UPLOADS_PER_ROLLING_WINDOW
from app.api.v1.endpoints import student_videos as student_videos_module
from app.core.file_validator import (
    ALLOWED_THUMBNAIL_EXTENSIONS,
    MAX_THUMBNAIL_SIZE_BYTES,
    MAX_VIDEO_SIZE_BYTES,
    validate_thumbnail_upload,
    validate_video_upload,
)
from app.db.session import get_db
from app.main import app
from app.models.enums import StudentVideoStatus
from app.models.student_videos import StudentVideo
from app.models.user import User

# Deliberately NOT a module-level `pytestmark` -- `TestFileValidatorUnit`
# below is entirely synchronous (it calls the validator functions
# directly, no `await` anywhere), and pytest-asyncio warns if `asyncio`
# marks a non-async test. Only `TestUploadRateLimitIntegration` (which
# genuinely awaits the async `db_session_factory`/`client_factory`
# fixtures) gets the marker, applied as its own class-level `pytestmark`
# below.


# ---------------------------------------------------------------------
# Fakes for TestFileValidatorUnit
# ---------------------------------------------------------------------


class _FakeCursorFile:
    """A minimal, seek/tell-only stand-in for the real spooled file
    `UploadFile.file` wraps -- reports a given `size` via `seek(0, 2)`/
    `.tell()` WITHOUT allocating that many actual bytes in memory. See
    module docstring for why this exists (affording the 2GB boundary
    test).
    """

    def __init__(self, size: int):
        self._size = size
        self._pos = 0

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 2:  # os.SEEK_END
            self._pos = self._size + offset
        elif whence == 0:  # os.SEEK_SET
            self._pos = offset
        else:  # os.SEEK_CUR
            self._pos += offset
        return self._pos

    def tell(self) -> int:
        return self._pos


@dataclass
class _FakeUploadFile:
    """Minimal duck-typed stand-in for `fastapi.UploadFile`, exposing
    only the three attributes `app/core/file_validator.py` actually
    reads (`content_type`, `filename`, `file`). Deliberately not a real
    `starlette.datastructures.UploadFile` -- constructing one requires
    threading a `Headers` object through a version-sensitive constructor
    just to set `content_type`, unnecessary indirection for testing a
    pure function that only ever reads these three plain attributes.
    """

    content_type: str
    filename: str
    file: object


# ---------------------------------------------------------------------
# TestFileValidatorUnit
# ---------------------------------------------------------------------


class TestFileValidatorUnit:
    # --- 413 Payload Too Large -----------------------------------------

    def test_video_exceeding_2gb_raises_413(self):
        oversized = _FakeCursorFile(MAX_VIDEO_SIZE_BYTES + 1)
        upload = _FakeUploadFile(content_type="video/mp4", filename="movie.mp4", file=oversized)
        with pytest.raises(HTTPException) as exc_info:
            validate_video_upload(upload)
        assert exc_info.value.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE

    def test_thumbnail_exceeding_5mb_raises_413(self):
        oversized_bytes = io.BytesIO(b"\x00" * (MAX_THUMBNAIL_SIZE_BYTES + 1))
        upload = _FakeUploadFile(content_type="image/jpeg", filename="thumb.jpg", file=oversized_bytes)
        with pytest.raises(HTTPException) as exc_info:
            validate_thumbnail_upload(upload)
        assert exc_info.value.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE

    def test_video_at_exactly_the_size_limit_is_accepted(self):
        """Boundary check: exactly `MAX_VIDEO_SIZE_BYTES` must NOT raise
        -- the limit is an inclusive ceiling ("maximum 2GB"), not an
        exclusive one."""
        exact = _FakeCursorFile(MAX_VIDEO_SIZE_BYTES)
        upload = _FakeUploadFile(content_type="video/mp4", filename="movie.mp4", file=exact)
        assert validate_video_upload(upload) == MAX_VIDEO_SIZE_BYTES

    def test_empty_video_raises_400_not_413(self):
        """A genuinely empty upload isn't "too large" -- it's a
        different, more specific problem, and gets a different status
        code."""
        empty = _FakeCursorFile(0)
        upload = _FakeUploadFile(content_type="video/mp4", filename="movie.mp4", file=empty)
        with pytest.raises(HTTPException) as exc_info:
            validate_video_upload(upload)
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    # --- 415 Unsupported Media Type -------------------------------------

    def test_invalid_video_content_type_raises_415(self):
        upload = _FakeUploadFile(
            content_type="application/octet-stream", filename="movie.mp4", file=io.BytesIO(b"data")
        )
        with pytest.raises(HTTPException) as exc_info:
            validate_video_upload(upload)
        assert exc_info.value.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE

    def test_video_webm_is_now_rejected(self):
        """Explicit regression test for this step's own disclosed
        narrowing (see `app/core/file_validator.py`'s module docstring):
        `video/webm` was accepted by the router's OLD inline allowlist,
        and must now be rejected under this step's stricter, literal
        spec (mp4/mov/avi only)."""
        upload = _FakeUploadFile(content_type="video/webm", filename="movie.webm", file=io.BytesIO(b"data"))
        with pytest.raises(HTTPException) as exc_info:
            validate_video_upload(upload)
        assert exc_info.value.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE

    @pytest.mark.parametrize(
        ("content_type", "filename"),
        [("video/mp4", "movie.mp4"), ("video/quicktime", "movie.mov"), ("video/x-msvideo", "movie.avi")],
    )
    def test_every_allowed_video_type_passes(self, content_type, filename):
        payload = b"a real-enough small video payload"
        upload = _FakeUploadFile(content_type=content_type, filename=filename, file=io.BytesIO(payload))
        assert validate_video_upload(upload) == len(payload)

    def test_double_extension_video_mp4_exe_raises_415(self):
        """The literal example from this step's own ask: a dangerous
        extension hidden as the TRUE final extension, behind a
        benign-looking `.mp4` earlier in the filename."""
        upload = _FakeUploadFile(
            content_type="video/mp4", filename="video.mp4.exe", file=io.BytesIO(b"malicious")
        )
        with pytest.raises(HTTPException) as exc_info:
            validate_video_upload(upload)
        assert exc_info.value.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE

    def test_reverse_double_extension_exe_mp4_raises_415(self):
        """Disclosed additional defense beyond the literal example: a
        dangerous extension hidden in the MIDDLE of the filename, behind
        an ALLOWED-looking final extension -- a check that only scans the
        final extension would miss this one."""
        upload = _FakeUploadFile(
            content_type="video/mp4", filename="payload.exe.mp4", file=io.BytesIO(b"malicious")
        )
        with pytest.raises(HTTPException) as exc_info:
            validate_video_upload(upload)
        assert exc_info.value.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE

    def test_invalid_thumbnail_content_type_raises_415(self):
        upload = _FakeUploadFile(content_type="image/gif", filename="thumb.gif", file=io.BytesIO(b"gif"))
        with pytest.raises(HTTPException) as exc_info:
            validate_thumbnail_upload(upload)
        assert exc_info.value.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE

    @pytest.mark.parametrize("ext", sorted(ALLOWED_THUMBNAIL_EXTENSIONS))
    def test_every_allowed_thumbnail_extension_passes(self, ext):
        content_type = {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
        payload = b"a real-enough small thumbnail payload"
        upload = _FakeUploadFile(content_type=content_type, filename=f"thumb{ext}", file=io.BytesIO(payload))
        assert validate_thumbnail_upload(upload) == len(payload)

    def test_webp_thumbnail_is_accepted(self):
        """Explicit, named regression test for this step's re-stated
        spec: `image/webp` must be accepted, not just silently covered by
        the parametrized sweep above (which was itself buggy before this
        fix -- see this file's own updated content-type mapping)."""
        payload = b"a real-enough small webp payload"
        upload = _FakeUploadFile(content_type="image/webp", filename="thumb.webp", file=io.BytesIO(payload))
        assert validate_thumbnail_upload(upload) == len(payload)


# ---------------------------------------------------------------------
# Fixtures for TestUploadRateLimitIntegration
# ---------------------------------------------------------------------

_CREATE_USERS_TABLE_SQL = """
CREATE TABLE users (
    id VARCHAR(36) PRIMARY KEY,
    email VARCHAR(255) UNIQUE,
    hashed_password VARCHAR(255),
    full_name VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT 1,
    is_superuser BOOLEAN NOT NULL DEFAULT 0,
    phone VARCHAR(50),
    degree_level VARCHAR(100),
    major VARCHAR(255),
    gpa FLOAT,
    graduation_year INTEGER,
    skills TEXT,
    target_country VARCHAR(120),
    age_group VARCHAR(50),
    age INTEGER,
    academic_level VARCHAR(20),
    gender VARCHAR(10),
    bio TEXT,
    search_vector TEXT,
    role VARCHAR(20) NOT NULL DEFAULT 'student',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Required because `User.bookmarks` uses `lazy="selectin"` (app/models/user.py)
# -- an EAGER batched query issued the moment any `User` row is loaded via
# the ORM (not a true lazy-load deferred until first access), so
# `session.get(User, ...)` in `_override_get_current_user` below fails
# with "no such table: user_bookmarks" without this, even though nothing
# in this test file ever reads `current_user.bookmarks` directly. Same
# table `tests/test_student_videos_my_uploads.py` already had to include
# for the identical reason.
_CREATE_USER_BOOKMARKS_TABLE_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    opportunity_id VARCHAR(36) NOT NULL,
    course_id VARCHAR(36),
    status VARCHAR(20) NOT NULL DEFAULT 'saved',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Same raw-SQL `student_videos` table as `tests/test_student_videos_my_uploads.py`
# -- required because `StudentVideo.ai_feedback` is `JSONB`, which
# SQLite's DDL compiler cannot render (see that file's own docstring for
# the full disclosed reasoning).
_CREATE_STUDENT_VIDEOS_TABLE_SQL = """
CREATE TABLE student_videos (
    id VARCHAR(36) PRIMARY KEY,
    student_id VARCHAR(36) NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    hls_master_url VARCHAR(500),
    status VARCHAR(20) NOT NULL DEFAULT 'pending_ai',
    ai_score INTEGER,
    ai_safety_flag BOOLEAN NOT NULL DEFAULT 0,
    ai_feedback TEXT,
    summary_embedding TEXT,
    duration_seconds INTEGER,
    transcript_text TEXT,
    transcript_segments TEXT,
    thumbnail_url VARCHAR(500),
    admin_override BOOLEAN NOT NULL DEFAULT 0,
    admin_notes TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CREATE_USERS_TABLE_SQL)
        await conn.exec_driver_sql(_CREATE_USER_BOOKMARKS_TABLE_SQL)
        await conn.exec_driver_sql(_CREATE_STUDENT_VIDEOS_TABLE_SQL)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name="Test Student")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _insert_video(db: AsyncSession, *, student_id, created_at) -> uuid.UUID:
    """Inserts via the ORM, not raw SQL -- same established reasoning as
    `test_student_videos_my_uploads.py`'s own `_insert_video` (JSONB
    columns need SQLAlchemy's own bind processors to round-trip
    correctly, which only real ORM-mediated writes go through)."""
    video_id = uuid.uuid4()
    video = StudentVideo(
        id=video_id,
        student_id=student_id,
        title="A previously uploaded video",
        status=StudentVideoStatus.PENDING_AI.value,
        created_at=created_at,
    )
    db.add(video)
    await db.commit()
    return video_id


def _valid_upload_multipart():
    """A minimal, VALID multipart payload for `POST /student/videos/upload`
    -- passes every file-validation check (real `video/mp4` content type,
    `.mp4` extension, well under the 2GB size limit) so any 429 the
    rate limiter raises is unambiguously attributable to the rate limit
    itself, not an incidental validation failure."""
    return (
        {"title": "A Perfectly Valid Title", "description": "A short description."},
        {"video": ("movie.mp4", b"a real-enough small video payload", "video/mp4")},
    )


class TestUploadRateLimitIntegration:
    """Exercises `check_upload_rate_limit` through the REAL router
    endpoint via `TestClient`, same `get_current_user`/`get_db`
    dependency-override technique as `tests/test_student_videos_my_uploads.py`.
    """

    pytestmark = pytest.mark.asyncio

    @pytest.fixture
    def client_factory(self, db_session_factory):
        async def _build():
            async with db_session_factory() as setup_session:
                user = await _make_user(setup_session)

            async def _override_get_db():
                async with db_session_factory() as session:
                    yield session

            async def _override_get_current_user():
                async with db_session_factory() as session:
                    return await session.get(User, user.id)

            app.dependency_overrides[get_db] = _override_get_db
            app.dependency_overrides[get_current_user] = _override_get_current_user

            return TestClient(app), user

        yield _build
        app.dependency_overrides.clear()

    async def test_429_after_3_uploads_within_24_hours(self, db_session_factory, client_factory):
        """This step's own explicit verification requirement: after 3
        mock videos already exist for this user within the rolling
        24-hour window, a 4th upload attempt is rejected with 429 --
        BEFORE any Celery task is ever dispatched."""
        client, user = await client_factory()
        now = datetime.now(timezone.utc)

        async with db_session_factory() as session:
            for hours_ago in (1, 5, 10):
                await _insert_video(session, student_id=user.id, created_at=now - timedelta(hours=hours_ago))

        assert MAX_UPLOADS_PER_ROLLING_WINDOW == 3, "test assumes the literal 3-per-24h limit from this step's ask"

        data, files = _valid_upload_multipart()
        with patch.object(student_videos_module.process_student_video_pipeline_task, "delay") as mock_delay:
            response = client.post("/api/v1/student/videos/upload", data=data, files=files)

        assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert "3 videos per 24 hours" in response.json()["detail"]
        # The crux of "before initiating the background Celery task": the
        # dispatch itself must never have been reached.
        mock_delay.assert_not_called()

    async def test_upload_succeeds_with_only_2_prior_uploads(self, db_session_factory, client_factory):
        """Positive control: the boundary is exactly 3, not an
        off-by-one that blocks a perfectly legitimate 3rd upload."""
        client, user = await client_factory()
        now = datetime.now(timezone.utc)

        async with db_session_factory() as session:
            for hours_ago in (1, 5):
                await _insert_video(session, student_id=user.id, created_at=now - timedelta(hours=hours_ago))

        data, files = _valid_upload_multipart()
        with patch.object(student_videos_module.process_student_video_pipeline_task, "delay") as mock_delay:
            mock_delay.return_value.id = "fake-task-id"
            response = client.post("/api/v1/student/videos/upload", data=data, files=files)

        assert response.status_code == status.HTTP_202_ACCEPTED
        mock_delay.assert_called_once()

    async def test_uploads_older_than_24_hours_do_not_count_toward_the_limit(
        self, db_session_factory, client_factory
    ):
        """Rolling-window correctness: 3 uploads that happened MORE than
        24 hours ago must not block a new one -- this is a true rolling
        window, not a "3 uploads ever" cap."""
        client, user = await client_factory()
        now = datetime.now(timezone.utc)

        async with db_session_factory() as session:
            for hours_ago in (25, 30, 48):
                await _insert_video(session, student_id=user.id, created_at=now - timedelta(hours=hours_ago))

        data, files = _valid_upload_multipart()
        with patch.object(student_videos_module.process_student_video_pipeline_task, "delay") as mock_delay:
            mock_delay.return_value.id = "fake-task-id"
            response = client.post("/api/v1/student/videos/upload", data=data, files=files)

        assert response.status_code == status.HTTP_202_ACCEPTED
        mock_delay.assert_called_once()

    async def test_413_through_the_real_endpoint_for_oversized_thumbnail(
        self, db_session_factory, client_factory
    ):
        """Integration-level 413 check (complementing `TestFileValidatorUnit`'s
        isolated-function tests): proves the real router wiring itself,
        not just the standalone validator function, rejects an oversized
        upload -- using the 5MB thumbnail limit (cheap to construct a
        real 5MB+1-byte payload) rather than the 2GB video limit, for the
        same "don't allocate gigabytes in a test" reasoning as
        `TestFileValidatorUnit`.
        """
        client, _user = await client_factory()
        data = {"title": "A Perfectly Valid Title"}
        files = {
            "video": ("movie.mp4", b"a real-enough small video payload", "video/mp4"),
            "thumbnail": ("thumb.jpg", b"\x00" * (MAX_THUMBNAIL_SIZE_BYTES + 1), "image/jpeg"),
        }
        with patch.object(student_videos_module.process_student_video_pipeline_task, "delay") as mock_delay:
            response = client.post("/api/v1/student/videos/upload", data=data, files=files)

        assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        mock_delay.assert_not_called()

    async def test_415_through_the_real_endpoint_for_invalid_video_type(
        self, db_session_factory, client_factory
    ):
        """Integration-level 415 check, same reasoning as the 413 test
        above."""
        client, _user = await client_factory()
        data = {"title": "A Perfectly Valid Title"}
        files = {"video": ("movie.exe", b"not a real video", "application/x-msdownload")}
        with patch.object(student_videos_module.process_student_video_pipeline_task, "delay") as mock_delay:
            response = client.post("/api/v1/student/videos/upload", data=data, files=files)

        assert response.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
        mock_delay.assert_not_called()
