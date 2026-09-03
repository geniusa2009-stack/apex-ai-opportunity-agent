"""Behavioral tests for the Cloud Garbage Collection (IPFS Unpinning &
R2 Deletion) step -- `app/services/storage_cleanup.py`'s
`StorageCleanupService`, `app/workers/cleanup_tasks.py`'s
`task_delete_orphaned_media`, and this step's two router wiring points
(`student_videos.py`'s `delete_student_video`, `admin_moderation.py`'s
new `delete_video_permanently`).

Per this step's own explicit verification requirement, three things are
covered: the Celery task correctly routes R2 urls to
`delete_from_r2`/CIDs to `unpin_from_pinata` without crashing on a
per-item failure; `StorageCleanupService`'s own two methods gracefully
handle "already gone" (404-shaped) responses from each provider without
raising; and both routers actually dispatch the cleanup task with the
right arguments at the right point in their own request lifecycle.

TEST DESIGN, disclosed: three classes.

`TestStorageCleanupServiceUnit` mocks `boto3`'s delete call (via
`storage_cleanup._r2_client`, the exact same external-I/O boundary
`r2_service.py`'s own tests would patch had any existed for it -- see
this file's own note on `r2_service.py` having no pre-existing test
coverage, a genuine, disclosed gap this step's tests do not attempt to
backfill for UPLOADS, only for the new DELETE path) and `httpx.Client`
(Pinata's unpin call), directly exercising `StorageCleanupService`'s two
methods -- config-missing, URL-mismatch, success, "already gone" (404 /
`NoSuchKey`), and genuine-failure paths for both.

`TestCleanupCeleryTaskRouting` calls `task_delete_orphaned_media(...)`
DIRECTLY (a `@celery_app.task`-decorated function remains a plain,
synchronously-callable Python function outside of `.delay()`/a real
broker -- the standard, established way to unit-test a Celery task's own
internal logic without needing a running worker) with
`StorageCleanupService.delete_from_r2`/`.unpin_from_pinata` mocked at
that exact boundary, proving the task correctly tallies successes/
failures per backend and never raises even when every single call fails.

`TestRouterDispatchesCleanupTask` exercises the two real HTTP DELETE
endpoints via `TestClient` (same `get_current_user`/`get_db`
dependency-override technique as `tests/test_admin_moderation.py`), with
ONLY `task_delete_orphaned_media.delay` mocked (never a real Celery
broker) -- same established pattern as `tests/test_security_shield.py`'s
own `patch.object(student_videos_module.process_student_video_pipeline_task,
"delay")`. Proves both routers capture the right URLs BEFORE the row is
gone, dispatch AFTER a successful commit (not before -- see each router's
own disclosed ordering-choice docstring), and skip dispatching entirely
for a video with no R2 urls at all (a still-`PENDING_AI` video).
"""

import uuid
from unittest.mock import MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from botocore.exceptions import BotoCoreError, ClientError
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.v1.endpoints import admin_moderation as admin_moderation_module
from app.services import dangerous_action_service as dangerous_action_service_module
from app.api.v1.endpoints import student_videos as student_videos_module
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.services import storage_cleanup as storage_cleanup_module
from app.services.storage_cleanup import StorageCleanupService
from app.workers.cleanup_tasks import task_delete_orphaned_media

# Deliberately NOT a module-level `pytestmark` -- `TestStorageCleanupServiceUnit`
# and `TestCleanupCeleryTaskRouting` are entirely synchronous; only
# `TestRouterDispatchesCleanupTask` genuinely awaits async fixtures. Same
# established split as `test_security_shield.py`.


# ---------------------------------------------------------------------
# TestStorageCleanupServiceUnit
# ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configure_r2_and_pinata(monkeypatch):
    """Every test in this file gets a fully-configured R2/Pinata by
    default -- individual tests that need to exercise the "not
    configured" path override one attribute back to `None` themselves.
    """
    monkeypatch.setattr(settings, "r2_account_id", "test-account-id")
    monkeypatch.setattr(settings, "r2_access_key_id", "test-access-key")
    monkeypatch.setattr(settings, "r2_secret_access_key", "test-secret-key")
    monkeypatch.setattr(settings, "r2_bucket_name", "test-bucket")
    monkeypatch.setattr(settings, "r2_public_base_url", "https://pub-test.r2.dev")
    monkeypatch.setattr(settings, "pinata_jwt", "test-pinata-jwt")


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, "DeleteObject")


class TestStorageCleanupServiceUnit:
    # --- delete_from_r2 --------------------------------------------------

    def test_delete_from_r2_success_extracts_correct_bucket_and_key(self):
        service = StorageCleanupService()
        mock_client = MagicMock()
        with patch.object(storage_cleanup_module, "_r2_client", return_value=mock_client):
            result = service.delete_from_r2(
                "https://pub-test.r2.dev/student-videos/abc123/hls/master.m3u8"
            )

        assert result is True
        mock_client.delete_object.assert_called_once_with(
            Bucket="test-bucket", Key="student-videos/abc123/hls/master.m3u8"
        )

    def test_delete_from_r2_not_configured_returns_false_without_calling_boto3(self, monkeypatch):
        monkeypatch.setattr(settings, "r2_account_id", None)
        service = StorageCleanupService()
        with patch.object(storage_cleanup_module, "_r2_client") as mock_client_factory:
            result = service.delete_from_r2("https://pub-test.r2.dev/some/key.jpg")

        assert result is False
        mock_client_factory.assert_not_called()

    def test_delete_from_r2_url_not_matching_base_returns_false_without_calling_boto3(self):
        """A URL that doesn't start with this deployment's own
        `r2_public_base_url` must never be blindly parsed into a "best
        guess" key and deleted -- see `_extract_r2_key`'s own docstring.
        """
        service = StorageCleanupService()
        with patch.object(storage_cleanup_module, "_r2_client") as mock_client_factory:
            result = service.delete_from_r2("https://some-other-host.example.com/key.jpg")

        assert result is False
        mock_client_factory.assert_not_called()

    def test_delete_from_r2_nosuchkey_client_error_treated_as_success(self):
        """This step's own explicit "gracefully handle 404s" requirement
        -- an already-gone object must not be reported as a failure."""
        service = StorageCleanupService()
        mock_client = MagicMock()
        mock_client.delete_object.side_effect = _client_error("NoSuchKey")
        with patch.object(storage_cleanup_module, "_r2_client", return_value=mock_client):
            result = service.delete_from_r2("https://pub-test.r2.dev/already/gone.jpg")

        assert result is True

    def test_delete_from_r2_other_client_error_returns_false_without_raising(self):
        service = StorageCleanupService()
        mock_client = MagicMock()
        mock_client.delete_object.side_effect = _client_error("AccessDenied")
        with patch.object(storage_cleanup_module, "_r2_client", return_value=mock_client):
            result = service.delete_from_r2("https://pub-test.r2.dev/forbidden.jpg")

        assert result is False

    def test_delete_from_r2_botocore_connection_error_returns_false_without_raising(self):
        """A network-level failure (R2 unreachable) -- the OTHER real
        `boto3` exception family, distinct from a structured
        `ClientError`; must be caught identically and never crash the
        caller."""
        service = StorageCleanupService()
        mock_client = MagicMock()
        mock_client.delete_object.side_effect = BotoCoreError()
        with patch.object(storage_cleanup_module, "_r2_client", return_value=mock_client):
            result = service.delete_from_r2("https://pub-test.r2.dev/unreachable.jpg")

        assert result is False

    # --- unpin_from_pinata -------------------------------------------------

    def test_unpin_from_pinata_success(self):
        service = StorageCleanupService()
        mock_response = MagicMock(status_code=200, is_success=True)
        with patch.object(httpx.Client, "delete", return_value=mock_response) as mock_delete:
            result = service.unpin_from_pinata("QmTestCid123")

        assert result is True
        called_url = mock_delete.call_args.args[0]
        assert called_url == "https://api.pinata.cloud/pinning/unpin/QmTestCid123"
        called_headers = mock_delete.call_args.kwargs["headers"]
        assert called_headers["Authorization"] == "Bearer test-pinata-jwt"

    def test_unpin_from_pinata_404_treated_as_success(self):
        """This step's own explicit "gracefully handle 404s" requirement,
        Pinata's side -- a CID that isn't (or is no longer) pinned under
        this account is a successful no-op, not a failure."""
        service = StorageCleanupService()
        mock_response = MagicMock(status_code=404, is_success=False, text='{"error": "PIN_NOT_FOUND"}')
        with patch.object(httpx.Client, "delete", return_value=mock_response):
            result = service.unpin_from_pinata("QmAlreadyUnpinned")

        assert result is True

    def test_unpin_from_pinata_other_failure_returns_false_without_raising(self):
        service = StorageCleanupService()
        mock_response = MagicMock(status_code=401, is_success=False, text="Invalid JWT")
        with patch.object(httpx.Client, "delete", return_value=mock_response):
            result = service.unpin_from_pinata("QmSomeCid")

        assert result is False

    def test_unpin_from_pinata_not_configured_returns_false_without_calling_pinata(self, monkeypatch):
        monkeypatch.setattr(settings, "pinata_jwt", None)
        service = StorageCleanupService()
        with patch.object(httpx.Client, "delete") as mock_delete:
            result = service.unpin_from_pinata("QmSomeCid")

        assert result is False
        mock_delete.assert_not_called()

    def test_unpin_from_pinata_timeout_returns_false_without_raising(self):
        service = StorageCleanupService()
        with patch.object(httpx.Client, "delete", side_effect=httpx.TimeoutException("timed out")):
            result = service.unpin_from_pinata("QmSlowCid")

        assert result is False

    def test_unpin_from_pinata_connection_error_returns_false_without_raising(self):
        service = StorageCleanupService()
        with patch.object(httpx.Client, "delete", side_effect=httpx.ConnectError("unreachable")):
            result = service.unpin_from_pinata("QmUnreachableCid")

        assert result is False


# ---------------------------------------------------------------------
# TestCleanupCeleryTaskRouting
# ---------------------------------------------------------------------


class TestCleanupCeleryTaskRouting:
    """`task_delete_orphaned_media` itself, called directly (not via
    `.delay`) -- proves it correctly routes R2 urls to `delete_from_r2`
    and CIDs to `unpin_from_pinata`, tallies per-item success/failure,
    and never raises even when every call fails.
    """

    def test_routes_r2_urls_and_ipfs_cids_to_the_right_methods(self):
        with (
            patch.object(StorageCleanupService, "delete_from_r2", return_value=True) as mock_r2,
            patch.object(StorageCleanupService, "unpin_from_pinata", return_value=True) as mock_pinata,
        ):
            result = task_delete_orphaned_media(
                r2_urls=["https://pub-test.r2.dev/a.mp4", "https://pub-test.r2.dev/b.jpg"],
                ipfs_cids=["QmCidOne"],
            )

        assert mock_r2.call_count == 2
        mock_r2.assert_any_call("https://pub-test.r2.dev/a.mp4")
        mock_r2.assert_any_call("https://pub-test.r2.dev/b.jpg")
        mock_pinata.assert_called_once_with("QmCidOne")
        assert result == {"r2_deleted": 2, "r2_failed": 0, "ipfs_unpinned": 1, "ipfs_failed": 0}

    def test_counts_partial_failures_without_raising(self):
        """One bad URL/CID must not abort the batch -- every item is
        still attempted, and the failure is reflected only in the
        returned counts."""
        with (
            patch.object(StorageCleanupService, "delete_from_r2", side_effect=[True, False]) as mock_r2,
            patch.object(StorageCleanupService, "unpin_from_pinata", return_value=False) as mock_pinata,
        ):
            result = task_delete_orphaned_media(
                r2_urls=["https://pub-test.r2.dev/good.mp4", "https://pub-test.r2.dev/bad.mp4"],
                ipfs_cids=["QmFailingCid"],
            )

        assert mock_r2.call_count == 2
        mock_pinata.assert_called_once()
        assert result == {"r2_deleted": 1, "r2_failed": 1, "ipfs_unpinned": 0, "ipfs_failed": 1}

    def test_defaults_to_empty_lists_and_does_nothing(self):
        """The ask's own literal signature defaults both parameters --
        calling with neither must be a safe, total no-op, not a
        `TypeError` from iterating over `None`."""
        with (
            patch.object(StorageCleanupService, "delete_from_r2") as mock_r2,
            patch.object(StorageCleanupService, "unpin_from_pinata") as mock_pinata,
        ):
            result = task_delete_orphaned_media()

        mock_r2.assert_not_called()
        mock_pinata.assert_not_called()
        assert result == {"r2_deleted": 0, "r2_failed": 0, "ipfs_unpinned": 0, "ipfs_failed": 0}

    def test_only_r2_urls_leaves_ipfs_untouched(self):
        """The overwhelmingly common real call shape (a `StudentVideo`
        delete, which only ever has R2 urls, never IPFS CIDs post-
        migration) -- confirms `unpin_from_pinata` is never called at all
        when `ipfs_cids` is omitted."""
        with (
            patch.object(StorageCleanupService, "delete_from_r2", return_value=True),
            patch.object(StorageCleanupService, "unpin_from_pinata") as mock_pinata,
        ):
            result = task_delete_orphaned_media(r2_urls=["https://pub-test.r2.dev/only.mp4"])

        mock_pinata.assert_not_called()
        assert result["r2_deleted"] == 1
        assert result["ipfs_unpinned"] == 0


# ---------------------------------------------------------------------
# TestRouterDispatchesCleanupTask -- real HTTP layer
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
    admin_role VARCHAR(20),
    role_granted_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Present only because `User.bookmarks` is `lazy="selectin"` -- same
# established reason every other test file touching `User` needs this
# empty table (see `test_admin_moderation.py`'s own identical comment).
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

# Verbatim column list from `test_admin_moderation.py`'s own
# `_CREATE_STUDENT_VIDEOS_TABLE_SQL` -- same continuing discipline.
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
    claimed_by VARCHAR(36),
    claimed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# NEW as of the Automatic Moderation Activity Log step: the admin-delete
# path this file exercises (`delete_video_permanently`) now writes a
# real `ModerationLogEntry` row in the same transaction as the video
# deletion. Same "JSONB has no SQLite DDL compiler" raw-SQL pattern as
# elsewhere.
_CREATE_MODERATION_LOG_TABLE_SQL = """
CREATE TABLE moderation_log_entries (
    id VARCHAR(36) PRIMARY KEY,
    actor_id VARCHAR(36),
    actor_role VARCHAR(20) NOT NULL,
    action VARCHAR(30) NOT NULL,
    target_type VARCHAR(20) NOT NULL,
    target_id VARCHAR(36) NOT NULL,
    outcome TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_DANGEROUS_ACTION_CONFIRMATIONS_TABLE_SQL = """
CREATE TABLE dangerous_action_confirmations (
    id VARCHAR(36) PRIMARY KEY,
    action VARCHAR(30) NOT NULL,
    item_type VARCHAR(30) NOT NULL,
    item_id VARCHAR(36) NOT NULL,
    notes TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    is_override BOOLEAN NOT NULL DEFAULT 0,
    requested_by VARCHAR(36),
    requested_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    executed_by VARCHAR(36),
    executed_at DATETIME
)
"""


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_TABLE_SQL))
        await conn.execute(text(_CREATE_STUDENT_VIDEOS_TABLE_SQL))
        await conn.execute(text(_CREATE_MODERATION_LOG_TABLE_SQL))
        await conn.execute(text(_CREATE_DANGEROUS_ACTION_CONFIRMATIONS_TABLE_SQL))

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession, *, is_superuser: bool = False) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name="Test User", is_superuser=is_superuser)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_video(db: AsyncSession, *, student_id: uuid.UUID, **overrides) -> StudentVideo:
    video_id = overrides.pop("id", uuid.uuid4())
    defaults = dict(id=video_id, student_id=student_id, title="A Test Video", status="approved")
    defaults.update(overrides)
    video = StudentVideo(**defaults)
    db.add(video)
    await db.commit()
    await db.refresh(video)
    return video


@pytest.fixture
def client_factory(db_session_factory):
    def _build(user: User) -> TestClient:
        async def _override_get_db():
            async with db_session_factory() as session:
                yield session

        async def _override_get_current_user():
            async with db_session_factory() as session:
                return await session.get(User, user.id)

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_current_user] = _override_get_current_user
        return TestClient(app)

    yield _build
    app.dependency_overrides.clear()


class TestRouterDispatchesCleanupTask:
    """Real HTTP DELETE requests through both routers, with ONLY
    `task_delete_orphaned_media.delay` mocked (no real Celery broker) --
    same established technique as `test_security_shield.py`'s own
    `process_student_video_pipeline_task.delay` mock.
    """

    pytestmark = pytest.mark.asyncio

    async def test_student_delete_dispatches_cleanup_with_both_urls(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session)
            video = await _make_video(
                session,
                student_id=student.id,
                hls_master_url="https://pub-test.r2.dev/student-videos/abc/hls/master.m3u8",
                thumbnail_url="https://pub-test.r2.dev/student-videos/abc/thumbnail.jpg",
            )

        client = client_factory(student)
        with patch.object(student_videos_module.task_delete_orphaned_media, "delay") as mock_delay:
            response = client.delete(f"/api/v1/student/videos/{video.id}")

        assert response.status_code == 204
        mock_delay.assert_called_once_with(
            r2_urls=[
                "https://pub-test.r2.dev/student-videos/abc/hls/master.m3u8",
                "https://pub-test.r2.dev/student-videos/abc/thumbnail.jpg",
            ]
        )

        # The crux of the disclosed ordering choice: the row must
        # already be gone by the time dispatch happens. Confirmed here
        # by re-querying fresh, proving the delete+commit genuinely
        # completed (not just that `.delay` was called).
        async with db_session_factory() as session:
            assert await session.get(StudentVideo, video.id) is None

    async def test_student_delete_of_pending_video_does_not_dispatch_cleanup(
        self, db_session_factory, client_factory
    ):
        """A still-`PENDING_AI` video legitimately has neither URL set
        yet -- nothing in R2 to clean up, so the task must not be
        dispatched at all (an empty-list dispatch would be a harmless
        but pointless Celery message)."""
        async with db_session_factory() as session:
            student = await _make_user(session)
            video = await _make_video(session, student_id=student.id, status="pending_ai")

        client = client_factory(student)
        with patch.object(student_videos_module.task_delete_orphaned_media, "delay") as mock_delay:
            response = client.delete(f"/api/v1/student/videos/{video.id}")

        assert response.status_code == 204
        mock_delay.assert_not_called()

    async def test_student_cannot_delete_another_students_video(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session)
            other_student = await _make_user(session)
            video = await _make_video(
                session, student_id=owner.id, hls_master_url="https://pub-test.r2.dev/owned/master.m3u8"
            )

        client = client_factory(other_student)
        with patch.object(student_videos_module.task_delete_orphaned_media, "delay") as mock_delay:
            response = client.delete(f"/api/v1/student/videos/{video.id}")

        assert response.status_code == 404
        mock_delay.assert_not_called()
        async with db_session_factory() as session:
            assert await session.get(StudentVideo, video.id) is not None

    async def test_admin_delete_dispatches_cleanup_and_removes_any_students_video(
        self, db_session_factory, client_factory
    ):
        """Real Two-Person Confirmation for Dangerous Actions addition:
        the real R2 cleanup dispatch now happens at APPROVAL time, from
        `dangerous_action_service.py` (where the mutation logic actually
        lives now -- see that module's own docstring), not from
        `admin_moderation.py` directly."""
        async with db_session_factory() as session:
            admin = await _make_user(session, is_superuser=True)
            second_admin = await _make_user(session, is_superuser=True)
            student = await _make_user(session)
            video = await _make_video(
                session,
                student_id=student.id,
                status="rejected",
                hls_master_url="https://pub-test.r2.dev/flagged/master.m3u8",
            )

        client = client_factory(admin)
        response = client.delete(f"/api/v1/admin/videos/{video.id}")
        assert response.status_code == 202
        confirmation_id = response.json()["confirmation_id"]

        approver_client = client_factory(second_admin)
        with patch.object(dangerous_action_service_module.task_delete_orphaned_media, "delay") as mock_delay:
            approve_response = approver_client.post(
                f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve"
            )

        assert approve_response.status_code == 200
        mock_delay.assert_called_once_with(r2_urls=["https://pub-test.r2.dev/flagged/master.m3u8"])

        async with db_session_factory() as session:
            assert await session.get(StudentVideo, video.id) is None

    async def test_non_admin_gets_403_on_admin_delete_and_nothing_is_dispatched(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            student = await _make_user(session, is_superuser=False)
            video = await _make_video(
                session, student_id=student.id, hls_master_url="https://pub-test.r2.dev/protected/master.m3u8"
            )

        client = client_factory(student)
        with patch.object(admin_moderation_module.task_delete_orphaned_media, "delay") as mock_delay:
            response = client.delete(f"/api/v1/admin/videos/{video.id}")

        assert response.status_code == 403
        mock_delay.assert_not_called()
        async with db_session_factory() as session:
            assert await session.get(StudentVideo, video.id) is not None

    async def test_admin_delete_of_nonexistent_video_returns_404(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            admin = await _make_user(session, is_superuser=True)

        client = client_factory(admin)
        with patch.object(admin_moderation_module.task_delete_orphaned_media, "delay") as mock_delay:
            response = client.delete(f"/api/v1/admin/videos/{uuid.uuid4()}")

        assert response.status_code == 404
        mock_delay.assert_not_called()
