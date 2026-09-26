"""Opportunity Monitoring & Personal Impact Engine, Part W's own test
matrix: CHANGE DETECTION, VERIFICATION, PERSONAL IMPACT, APPLICATION
IMPACT, NOTIFICATIONS, SOURCE FAILURES, SCHEDULING, GUIDANCE
INTEGRATION, USER ISOLATION, PERFORMANCE/CORRECTNESS.

Same "minimal raw-SQL SQLite tables" fixture convention as
`test_application_intelligence.py`/`test_user_intelligence.py`, extended
with `opportunity_change_history.severity`, `opportunities.
monitoring_last_checked_at`, `user_notification_preferences`, and
`notifications`.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.application_tracker import ApplicationTracker
from app.models.bookmark import UserBookmark
from app.models.enums import BookmarkStatus, ChangeSeverity, TrackerStatus, VerificationDecisionStatus
from app.models.notification import Notification
from app.models.opportunity import Opportunity
from app.models.opportunity_change_history import OpportunityChangeHistory
from app.models.user_notification_preference import UserNotificationPreference
from app.services import notification_preferences
from app.services.guidance_service import build_guidance
from app.services.opportunity_change_notifications import create_change_notifications_for_opportunity
from app.services.opportunity_monitoring import (
    DetectedChange,
    classify_change_severity,
    detect_meaningful_changes,
    record_verified_changes,
    verify_change_candidate,
)
from app.services.opportunity_monitoring_pipeline import process_opportunity_candidate
from app.services.opportunity_monitoring_scheduler import find_opportunities_to_monitor
from app.services.opportunity_reverification import reverify_opportunity
from app.services.personal_impact_service import find_affected_users

_CREATE_USERS_SQL = """
CREATE TABLE users (
    id VARCHAR(36) PRIMARY KEY, email VARCHAR(255) UNIQUE, hashed_password VARCHAR(255),
    full_name VARCHAR(255), is_active BOOLEAN NOT NULL DEFAULT 1, is_superuser BOOLEAN NOT NULL DEFAULT 0,
    phone VARCHAR(50), degree_level VARCHAR(100), major VARCHAR(255), gpa FLOAT, graduation_year INTEGER,
    skills TEXT, target_country VARCHAR(120), age_group VARCHAR(50), age INTEGER, academic_level VARCHAR(20),
    gender VARCHAR(10), bio TEXT, search_vector TEXT, role VARCHAR(20) NOT NULL DEFAULT 'student',
    admin_role VARCHAR(20), role_granted_at DATETIME,
    nationality VARCHAR(120), residency_country VARCHAR(120), languages TEXT,
    interests TEXT, application_goals TEXT, preferred_opportunity_types TEXT,
    remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_SOURCES_SQL = """
CREATE TABLE sources (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL, base_url VARCHAR(2048) NOT NULL,
    active_status BOOLEAN NOT NULL DEFAULT 1, source_type VARCHAR(20) NOT NULL DEFAULT 'website',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_OPPORTUNITIES_SQL = """
CREATE TABLE opportunities (
    id VARCHAR(36) PRIMARY KEY, title VARCHAR(500) NOT NULL, description TEXT,
    target_url VARCHAR(2048) NOT NULL, source_id VARCHAR(36) NOT NULL, type VARCHAR(20) NOT NULL,
    deadline DATETIME, eligibility_criteria TEXT, funding_details TEXT, location VARCHAR(255),
    country VARCHAR(120) DEFAULT 'Global', is_verified BOOLEAN NOT NULL DEFAULT 0, ai_confidence_score FLOAT,
    raw_extracted_data TEXT, embedding TEXT, is_local_egypt BOOLEAN NOT NULL DEFAULT 0,
    target_age_group VARCHAR(50), target_academic_levels TEXT, min_age INTEGER, max_age INTEGER,
    target_gender VARCHAR(10), review_status VARCHAR(20) NOT NULL DEFAULT 'published',
    claimed_by VARCHAR(36), claimed_at DATETIME,
    lifecycle_state VARCHAR(30) NOT NULL DEFAULT 'discovered',
    lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME,
    monitoring_last_checked_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_USER_BOOKMARKS_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36), course_id VARCHAR(36),
    status VARCHAR(20) NOT NULL DEFAULT 'saved', created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_APPLICATION_TRACKERS_SQL = """
CREATE TABLE application_trackers (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, scholarship_name VARCHAR(500) NOT NULL,
    institution VARCHAR(255), status VARCHAR(30) NOT NULL DEFAULT 'interested', deadline DATETIME, notes TEXT,
    ai_todo_list TEXT NOT NULL DEFAULT '[]', opportunity_id VARCHAR(36), submitted_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_OPPORTUNITY_CHANGE_HISTORY_SQL = """
CREATE TABLE opportunity_change_history (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, field_name VARCHAR(50) NOT NULL,
    old_value TEXT, new_value TEXT, source_id VARCHAR(36), evidence_id VARCHAR(36),
    change_reason TEXT NOT NULL, severity VARCHAR(20), changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_USER_NOTIFICATION_PREFERENCES_SQL = """
CREATE TABLE user_notification_preferences (
    user_id VARCHAR(36) PRIMARY KEY, opportunity_changes BOOLEAN NOT NULL DEFAULT 1,
    deadline_alerts BOOLEAN NOT NULL DEFAULT 1, application_alerts BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_NOTIFICATIONS_SQL = """
CREATE TABLE notifications (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, type VARCHAR(50) NOT NULL,
    title VARCHAR(255) NOT NULL, message TEXT NOT NULL, is_read BOOLEAN NOT NULL DEFAULT 0,
    payload TEXT, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_OPPORTUNITY_INTERACTIONS_SQL = """
CREATE TABLE opportunity_interactions (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36) NOT NULL,
    event_type VARCHAR(30) NOT NULL, event_metadata TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def db_session_factory(monkeypatch):
    # notification_service.create_notification's real-time broadcast step
    # (Redis) and transactional-email dispatch (Celery) are both
    # best-effort side effects unrelated to what this test file verifies
    # (candidate selection, severity gating, dedup, preferences) -- same
    # "mock the boundary this phase doesn't own" posture as
    # `test_application_intelligence.py`'s own Gemini mock.
    from unittest.mock import AsyncMock

    import app.services.notification_service as notification_service_module

    monkeypatch.setattr(notification_service_module, "publish_notification", AsyncMock(return_value=None))
    monkeypatch.setattr(notification_service_module, "_dispatch_critical_email_best_effort", AsyncMock(return_value=None))

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_SQL))
        await conn.execute(text(_CREATE_SOURCES_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITIES_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_SQL))
        await conn.execute(text(_CREATE_APPLICATION_TRACKERS_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITY_CHANGE_HISTORY_SQL))
        await conn.execute(text(_CREATE_USER_NOTIFICATION_PREFERENCES_SQL))
        await conn.execute(text(_CREATE_NOTIFICATIONS_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITY_INTERACTIONS_SQL))
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _make_user(db: AsyncSession, **overrides) -> uuid.UUID:
    user_id = uuid.uuid4()
    defaults = {"id": user_id.hex, "email": f"{user_id.hex}@example.com", "hashed_password": "x", "full_name": "Test User"}
    defaults.update(overrides)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join(f":{k}" for k in defaults.keys())
    await db.execute(text(f"INSERT INTO users ({cols}) VALUES ({placeholders})"), defaults)
    await db.commit()
    return user_id


async def _make_source(db: AsyncSession) -> uuid.UUID:
    source_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO sources (id, name, base_url) VALUES (:id, :name, :base_url)"),
        {"id": source_id.hex, "name": "Test Source", "base_url": "https://example.com"},
    )
    await db.commit()
    return source_id


async def _make_opportunity(db: AsyncSession, *, source_id: uuid.UUID, **overrides) -> uuid.UUID:
    opp_id = uuid.uuid4()
    defaults = {
        "id": opp_id.hex, "title": "Some Opportunity", "target_url": "https://example.com/opp",
        "source_id": source_id.hex, "type": "scholarship", "review_status": "published",
        "lifecycle_state": "open", "country": "Egypt",
    }
    defaults.update(overrides)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join(f":{k}" for k in defaults.keys())
    await db.execute(text(f"INSERT INTO opportunities ({cols}) VALUES ({placeholders})"), defaults)
    await db.commit()
    return opp_id


async def _make_bookmark(db: AsyncSession, *, user_id: uuid.UUID, opportunity_id: uuid.UUID, status: str = "saved") -> None:
    await db.execute(
        text("INSERT INTO user_bookmarks (id, user_id, opportunity_id, status) VALUES (:id, :user_id, :opportunity_id, :status)"),
        {"id": uuid.uuid4().hex, "user_id": user_id.hex, "opportunity_id": opportunity_id.hex, "status": status},
    )
    await db.commit()


async def _make_tracker(
    db: AsyncSession, *, user_id: uuid.UUID, opportunity_id: uuid.UUID | None, status: str = "interested",
) -> uuid.UUID:
    tracker_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO application_trackers (id, user_id, scholarship_name, status, opportunity_id) "
            "VALUES (:id, :user_id, 'Some Opportunity', :status, :opportunity_id)"
        ),
        {"id": tracker_id.hex, "user_id": user_id.hex, "status": status,
         "opportunity_id": opportunity_id.hex if opportunity_id else None},
    )
    await db.commit()
    return tracker_id


# ============================================================
# CHANGE DETECTION
# ============================================================


class TestChangeDetection:
    def test_deadline_changed_is_detected(self):
        opp = Opportunity(deadline=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
        changes = detect_meaningful_changes(opp, {"deadline": dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)})
        assert len(changes) == 1
        assert changes[0].field_name == "deadline"

    def test_deadline_unchanged_is_not_detected(self):
        deadline = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        opp = Opportunity(deadline=deadline)
        changes = detect_meaningful_changes(opp, {"deadline": deadline})
        assert changes == []

    def test_funding_changed_is_detected(self):
        opp = Opportunity(funding_details="Full funding")
        changes = detect_meaningful_changes(opp, {"funding_details": "Partial funding"})
        assert len(changes) == 1 and changes[0].field_name == "funding_details"

    def test_eligibility_changed_is_detected(self):
        opp = Opportunity(eligibility_criteria="Open to Egypt")
        changes = detect_meaningful_changes(opp, {"eligibility_criteria": "Open to Egypt and Jordan"})
        assert len(changes) == 1 and changes[0].field_name == "eligibility_criteria"

    def test_lifecycle_changed_is_detected(self):
        opp = Opportunity(lifecycle_state="open")
        changes = detect_meaningful_changes(opp, {"lifecycle_state": "closed"})
        assert len(changes) == 1 and changes[0].field_name == "lifecycle_state"

    def test_field_absent_from_candidate_is_not_treated_as_changed(self):
        """A caller without fresh data for a field must never look like
        it observed that field going away (Part B/Q)."""
        opp = Opportunity(deadline=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc), country="Egypt")
        changes = detect_meaningful_changes(opp, {"country": "Egypt"})
        assert changes == []

    def test_unmonitored_field_is_never_detected(self):
        """`description` is deliberately excluded from MONITORED_FIELDS."""
        from app.services.opportunity_monitoring import MONITORED_FIELDS

        assert "description" not in MONITORED_FIELDS
        opp = Opportunity(description="Old text.")
        changes = detect_meaningful_changes(opp, {"description": "Old text!"})
        assert changes == []

    def test_multiple_fields_changed_in_one_candidate(self):
        opp = Opportunity(deadline=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc), country="Egypt")
        changes = detect_meaningful_changes(
            opp, {"deadline": dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc), "country": "Jordan"}
        )
        assert {c.field_name for c in changes} == {"deadline", "country"}

    def test_none_to_real_value_is_detected(self):
        opp = Opportunity(deadline=None)
        changes = detect_meaningful_changes(opp, {"deadline": dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)})
        assert len(changes) == 1


# ============================================================
# SEVERITY CLASSIFICATION
# ============================================================


class TestSeverityClassification:
    def test_deadline_appearing_is_high(self):
        assert classify_change_severity("deadline", None, dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)) == ChangeSeverity.HIGH

    def test_deadline_disappearing_is_high(self):
        assert classify_change_severity("deadline", dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc), None) == ChangeSeverity.HIGH

    def test_deadline_small_shift_is_minor(self):
        old = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        new = dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc)
        assert classify_change_severity("deadline", old, new) == ChangeSeverity.MINOR

    def test_deadline_moderate_shift_is_material(self):
        old = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        new = dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc)
        assert classify_change_severity("deadline", old, new) == ChangeSeverity.MATERIAL

    def test_deadline_large_shift_is_high(self):
        old = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        new = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)
        assert classify_change_severity("deadline", old, new) == ChangeSeverity.HIGH

    def test_deadline_unchanged_instant_is_ignore(self):
        d = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        assert classify_change_severity("deadline", d, d) == ChangeSeverity.IGNORE

    def test_lifecycle_open_to_closed_is_critical(self):
        assert classify_change_severity("lifecycle_state", "open", "closed") == ChangeSeverity.CRITICAL

    def test_lifecycle_closed_to_open_reopened_is_critical(self):
        assert classify_change_severity("lifecycle_state", "closed", "open") == ChangeSeverity.CRITICAL

    def test_lifecycle_other_transition_is_material(self):
        assert classify_change_severity("lifecycle_state", "discovered", "open") == ChangeSeverity.MATERIAL

    def test_eligibility_appearing_is_high(self):
        assert classify_change_severity("eligibility_criteria", None, "Open to Egypt") == ChangeSeverity.HIGH

    def test_eligibility_edit_between_real_values_is_material(self):
        assert classify_change_severity("eligibility_criteria", "Open to Egypt", "Open to Egypt.") == ChangeSeverity.MATERIAL

    def test_funding_fully_to_partial_is_material_not_ignore(self):
        # Free text -- this codebase cannot semantically diff prose
        # deterministically, so any real edit is at least MATERIAL
        # (disclosed, conservative default; see the module's own
        # docstring).
        severity = classify_change_severity("funding_details", "Fully funded", "Partial funding")
        assert severity in (ChangeSeverity.MATERIAL, ChangeSeverity.HIGH)

    def test_geography_field_appearing_is_high(self):
        assert classify_change_severity("country", None, "Egypt") == ChangeSeverity.HIGH

    def test_application_url_change_is_always_high(self):
        assert classify_change_severity("target_url", "https://old.example.com", "https://new.example.com") == ChangeSeverity.HIGH

    def test_review_status_change_is_critical(self):
        assert classify_change_severity("review_status", "published", "pending") == ChangeSeverity.CRITICAL

    def test_unknown_field_defaults_to_material_not_ignore(self):
        """Conservative default -- never silently IGNORE something the
        table hasn't been taught about (Part C's own disclosed rule)."""
        assert classify_change_severity("some_new_field_nobody_added_yet", "a", "b") == ChangeSeverity.MATERIAL


# ============================================================
# VERIFICATION
# ============================================================


class TestVerification:
    def test_structured_source_is_always_verified(self):
        assert verify_change_candidate(source_trust="structured") == VerificationDecisionStatus.VERIFIED

    def test_ai_reextraction_above_threshold_is_verified(self):
        status = verify_change_candidate(
            source_trust="ai_reextraction", ai_is_legitimate=True, ai_confidence_score=0.95, ai_confidence_threshold=0.7,
        )
        assert status == VerificationDecisionStatus.VERIFIED

    def test_ai_reextraction_below_threshold_is_held(self):
        status = verify_change_candidate(
            source_trust="ai_reextraction", ai_is_legitimate=True, ai_confidence_score=0.3, ai_confidence_threshold=0.7,
        )
        assert status == VerificationDecisionStatus.HOLD

    def test_ai_reextraction_not_legitimate_is_held_even_with_high_confidence(self):
        status = verify_change_candidate(
            source_trust="ai_reextraction", ai_is_legitimate=False, ai_confidence_score=0.99, ai_confidence_threshold=0.7,
        )
        assert status == VerificationDecisionStatus.HOLD

    def test_unknown_source_trust_raises(self):
        with pytest.raises(ValueError):
            verify_change_candidate(source_trust="vibes")

    @pytest.mark.asyncio
    async def test_hold_verification_records_nothing(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, deadline=None)
            changes = [DetectedChange(field_name="deadline", old_value=None, new_value=_now())]
            recorded = await record_verified_changes(
                db, opportunity_id=opp_id, changes=changes,
                verification_status=VerificationDecisionStatus.HOLD, change_reason="test",
            )
            assert recorded == []
            result = await db.execute(select(OpportunityChangeHistory).where(OpportunityChangeHistory.opportunity_id == opp_id))
            assert result.scalars().all() == []

    @pytest.mark.asyncio
    async def test_verified_change_is_recorded_with_severity(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, lifecycle_state="open")
            changes = [DetectedChange(field_name="lifecycle_state", old_value="open", new_value="closed")]
            recorded = await record_verified_changes(
                db, opportunity_id=opp_id, changes=changes,
                verification_status=VerificationDecisionStatus.VERIFIED, change_reason="test",
            )
            assert len(recorded) == 1
            assert recorded[0].severity == ChangeSeverity.CRITICAL
            assert recorded[0].history_row.severity == ChangeSeverity.CRITICAL.value

    @pytest.mark.asyncio
    async def test_ignore_severity_change_is_verified_but_not_recorded(self, db_session_factory):
        """A genuinely detected difference whose severity classifies to
        IGNORE must never create history noise (Part B/C)."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            d = _now()
            changes = [DetectedChange(field_name="deadline", old_value=d, new_value=d)]
            recorded = await record_verified_changes(
                db, opportunity_id=opp_id, changes=changes,
                verification_status=VerificationDecisionStatus.VERIFIED, change_reason="test",
            )
            assert recorded == []


# ============================================================
# PERSONAL IMPACT
# ============================================================


class TestPersonalImpact:
    @pytest.mark.asyncio
    async def test_saved_user_is_identified(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_id, status="saved")

            affected = await find_affected_users(db, opportunity_id=opp_id)
            assert len(affected) == 1
            assert affected[0].user_id == user_id
            assert affected[0].reasons[0].category == "saved"

    @pytest.mark.asyncio
    async def test_active_applicant_is_identified(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            await _make_tracker(db, user_id=user_id, opportunity_id=opp_id, status="preparing_docs")

            affected = await find_affected_users(db, opportunity_id=opp_id)
            assert len(affected) == 1
            assert affected[0].has_active_application is True
            assert any(r.category == "active_application" for r in affected[0].reasons)

    @pytest.mark.asyncio
    async def test_irrelevant_user_is_excluded(self, db_session_factory):
        async with db_session_factory() as db:
            await _make_user(db)  # no bookmark, no tracker
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)

            affected = await find_affected_users(db, opportunity_id=opp_id)
            assert affected == []

    @pytest.mark.asyncio
    async def test_viewed_only_user_is_not_identified(self, db_session_factory):
        """`OpportunityInteraction` VIEWED events are deliberately NOT a
        signal here (Part G's own explicit rule)."""
        async with db_session_factory() as db:
            await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            # No bookmark, no tracker -- a bare view (not modeled in this
            # fixture at all) must never surface as an affected user.
            affected = await find_affected_users(db, opportunity_id=opp_id)
            assert affected == []

    @pytest.mark.asyncio
    async def test_completed_application_is_not_active(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            await _make_tracker(db, user_id=user_id, opportunity_id=opp_id, status="applied")

            affected = await find_affected_users(db, opportunity_id=opp_id)
            assert affected == [] or affected[0].has_active_application is False

    @pytest.mark.asyncio
    async def test_multiple_relevance_reasons_are_both_present_not_deduplicated_away(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_id, status="saved")
            await _make_tracker(db, user_id=user_id, opportunity_id=opp_id, status="interested")

            affected = await find_affected_users(db, opportunity_id=opp_id)
            assert len(affected) == 1
            categories = {r.category for r in affected[0].reasons}
            assert categories == {"saved", "active_application"}

    @pytest.mark.asyncio
    async def test_user_isolation_in_impact_lookup(self, db_session_factory):
        async with db_session_factory() as db:
            user_a = await _make_user(db)
            user_b = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            await _make_bookmark(db, user_id=user_a, opportunity_id=opp_id)

            affected = await find_affected_users(db, opportunity_id=opp_id)
            user_ids = {a.user_id for a in affected}
            assert user_a in user_ids
            assert user_b not in user_ids


# ============================================================
# APPLICATION IMPACT
# ============================================================


class TestApplicationImpact:
    @pytest.mark.asyncio
    async def test_deadline_change_creates_history_for_active_application_opportunity(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, deadline="2026-10-01 00:00:00")
            await _make_tracker(db, user_id=user_id, opportunity_id=opp_id, status="preparing_docs")

            opp = await db.get(Opportunity, opp_id)
            result = await process_opportunity_candidate(
                db, opportunity=opp,
                candidate={"deadline": dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc)},
                source_trust="structured", change_reason="test",
            )
            assert len(result.recorded) == 1
            assert result.recorded[0].history_row.field_name == "deadline"

    @pytest.mark.asyncio
    async def test_application_state_is_not_silently_mutated_by_opportunity_change(self, db_session_factory):
        """Part I's own explicit rule: opportunity data changing must
        never automatically mutate `ApplicationTracker.status`."""
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, lifecycle_state="open")
            tracker_id = await _make_tracker(db, user_id=user_id, opportunity_id=opp_id, status="preparing_docs")

            opp = await db.get(Opportunity, opp_id)
            await process_opportunity_candidate(
                db, opportunity=opp, candidate={"lifecycle_state": "closed"},
                source_trust="structured", change_reason="test",
            )

            tracker = await db.get(ApplicationTracker, tracker_id)
            assert tracker.status == "preparing_docs"

    @pytest.mark.asyncio
    async def test_eligibility_change_is_recorded_for_active_application(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, eligibility_criteria="Open to Egypt")
            await _make_tracker(db, user_id=user_id, opportunity_id=opp_id, status="interested")

            opp = await db.get(Opportunity, opp_id)
            result = await process_opportunity_candidate(
                db, opportunity=opp, candidate={"eligibility_criteria": "Open to Egypt and Jordan only"},
                source_trust="structured", change_reason="test",
            )
            assert len(result.recorded) == 1
            assert result.recorded[0].severity in (ChangeSeverity.MATERIAL, ChangeSeverity.HIGH)


# ============================================================
# NOTIFICATIONS
# ============================================================


class TestNotifications:
    @pytest.mark.asyncio
    async def test_high_severity_change_notifies_affected_user(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, lifecycle_state="open", title="Big Scholarship")
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_id)

            opp = await db.get(Opportunity, opp_id)
            result = await process_opportunity_candidate(
                db, opportunity=opp, candidate={"lifecycle_state": "closed"},
                source_trust="structured", change_reason="test",
            )
            assert result.notifications_created == 1

            notes = (await db.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all()
            assert len(notes) == 1
            assert notes[0].type == "WARNING"

    @pytest.mark.asyncio
    async def test_material_severity_change_does_not_notify(self, db_session_factory):
        """MATERIAL reaches guidance/feed but not a push notification
        (Part C's trust-over-volume principle, applied concretely)."""
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, eligibility_criteria="Open to Egypt")
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_id)

            opp = await db.get(Opportunity, opp_id)
            result = await process_opportunity_candidate(
                db, opportunity=opp, candidate={"eligibility_criteria": "Open to Egypt, edited slightly"},
                source_trust="structured", change_reason="test",
            )
            assert result.recorded[0].severity == ChangeSeverity.MATERIAL
            assert result.notifications_created == 0

    @pytest.mark.asyncio
    async def test_duplicate_change_processing_does_not_duplicate_notification(self, db_session_factory):
        """Part N: the same real change, notified twice (e.g. a retried
        worker calling this twice for the identical history row), must
        not create two notifications."""
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, lifecycle_state="open")
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_id)

            opp = await db.get(Opportunity, opp_id)
            result = await process_opportunity_candidate(
                db, opportunity=opp, candidate={"lifecycle_state": "closed"},
                source_trust="structured", change_reason="test",
            )
            assert result.notifications_created == 1

            # Re-run notification creation for the SAME already-recorded
            # change (simulating a retry) -- must be a no-op.
            created_again = await create_change_notifications_for_opportunity(
                db, opportunity=opp, recorded_changes=result.recorded,
            )
            assert created_again == 0
            notes = (await db.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all()
            assert len(notes) == 1

    @pytest.mark.asyncio
    async def test_preference_opt_out_is_respected(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, lifecycle_state="open")
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_id)

            pref = await notification_preferences.get_or_create_preferences(db, user_id)
            pref.opportunity_changes = False
            db.add(pref)
            await db.commit()

            opp = await db.get(Opportunity, opp_id)
            result = await process_opportunity_candidate(
                db, opportunity=opp, candidate={"lifecycle_state": "closed"},
                source_trust="structured", change_reason="test",
            )
            assert result.notifications_created == 0

    @pytest.mark.asyncio
    async def test_no_row_preference_defaults_to_opted_in(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            opted_in = await notification_preferences.is_opted_in(db, user_id, "opportunity_changes")
            assert opted_in is True

    @pytest.mark.asyncio
    async def test_unknown_preference_category_raises(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            with pytest.raises(ValueError):
                await notification_preferences.is_opted_in(db, user_id, "not_a_real_category")

    @pytest.mark.asyncio
    async def test_no_affected_users_creates_zero_notifications(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, lifecycle_state="open")
            opp = await db.get(Opportunity, opp_id)
            result = await process_opportunity_candidate(
                db, opportunity=opp, candidate={"lifecycle_state": "closed"},
                source_trust="structured", change_reason="test",
            )
            assert result.notifications_created == 0


# ============================================================
# SOURCE FAILURES
# ============================================================


class TestSourceFailures:
    @pytest.mark.asyncio
    async def test_missing_raw_data_is_skipped_not_an_error(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, raw_extracted_data=None)
            opp = await db.get(Opportunity, opp_id)
            outcome = await reverify_opportunity(db, opportunity=opp)
            assert outcome.skipped_no_raw_data is True
            assert outcome.ai_call_failed is False

    @pytest.mark.asyncio
    async def test_ai_call_failure_does_not_change_opportunity_state(self, db_session_factory, monkeypatch):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(
                db, source_id=source_id, lifecycle_state="open", raw_extracted_data='{"title": "x"}',
            )
            opp = await db.get(Opportunity, opp_id)

            from unittest.mock import AsyncMock

            import app.services.opportunity_reverification as reverification_module

            monkeypatch.setattr(
                reverification_module.ai_gateway, "verify_and_embed", AsyncMock(side_effect=RuntimeError("boom")),
            )
            outcome = await reverify_opportunity(db, opportunity=opp)
            assert outcome.ai_call_failed is True
            assert outcome.cycle_result is None

            reloaded = await db.get(Opportunity, opp_id)
            assert reloaded.lifecycle_state == "open"  # completely untouched (Part Q)

    @pytest.mark.asyncio
    async def test_ai_call_failure_still_stamps_last_checked_at(self, db_session_factory, monkeypatch):
        """Operational bookkeeping only -- never opportunity content
        (Part Q) -- so this IS allowed to update even on failure."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, raw_extracted_data='{"title": "x"}')
            opp = await db.get(Opportunity, opp_id)

            from unittest.mock import AsyncMock

            import app.services.opportunity_reverification as reverification_module

            monkeypatch.setattr(
                reverification_module.ai_gateway, "verify_and_embed", AsyncMock(side_effect=RuntimeError("boom")),
            )
            await reverify_opportunity(db, opportunity=opp)

            reloaded = await db.get(Opportunity, opp_id)
            assert reloaded.monitoring_last_checked_at is not None

    @pytest.mark.asyncio
    async def test_hold_verification_from_low_confidence_reextraction_touches_nothing(self, db_session_factory, monkeypatch):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(
                db, source_id=source_id, eligibility_criteria="Original text", raw_extracted_data='{"title": "x"}',
            )
            opp = await db.get(Opportunity, opp_id)

            from dataclasses import dataclass
            from unittest.mock import AsyncMock

            import app.services.opportunity_reverification as reverification_module

            @dataclass
            class FakeVerification:
                deadline: str | None = None
                eligibility_criteria: str | None = "Wildly different text"
                funding_details: str | None = None
                min_age: int | None = None
                max_age: int | None = None
                target_gender: str | None = None
                target_academic_levels: list | None = None
                is_legitimate: bool = True
                confidence_score: float = 0.1  # below threshold

            monkeypatch.setattr(
                reverification_module.ai_gateway, "verify_and_embed",
                AsyncMock(return_value=(FakeVerification(), [0.0] * 8)),
            )
            outcome = await reverify_opportunity(db, opportunity=opp)
            assert outcome.ai_call_failed is False
            assert outcome.cycle_result.recorded == []

            result = await db.execute(select(OpportunityChangeHistory).where(OpportunityChangeHistory.opportunity_id == opp_id))
            assert result.scalars().all() == []


# ============================================================
# FULL INTELLIGENCE AUDIT REGRESSION TESTS
# ============================================================
# Real bugs found by this repo's own full-system audit (not part of the
# original Opportunity Monitoring phase's own test matrix). Each class
# below reproduces the exact failure scenario the audit's evidence
# described, confirms it against the pre-fix code would have failed,
# and pins the fixed behavior down as a regression test.


class TestReverificationAppliesVerifiedValues:
    """Audit finding (P1): `reverify_opportunity` recorded a VERIFIED,
    notifiable `OpportunityChangeHistory` row for a re-extracted field
    but never actually wrote the new value onto the `Opportunity` row
    itself -- the opportunity's own displayed data silently never
    updated, and every subsequent scheduled re-check would keep
    re-detecting the "same" real-world change forever (duplicate
    history rows / duplicate notifications on every cycle)."""

    @pytest.mark.asyncio
    async def test_verified_deadline_reextraction_updates_the_opportunity_row(self, db_session_factory, monkeypatch):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            old_deadline = _now() + dt.timedelta(days=10)
            opp_id = await _make_opportunity(
                db, source_id=source_id, deadline=old_deadline.isoformat(), raw_extracted_data='{"title": "x"}',
            )
            opp = await db.get(Opportunity, opp_id)

            from dataclasses import dataclass
            from unittest.mock import AsyncMock

            import app.services.opportunity_reverification as reverification_module

            new_deadline = _now() + dt.timedelta(days=40)

            @dataclass
            class FakeVerification:
                deadline: str | None = new_deadline.isoformat()
                eligibility_criteria: str | None = None
                funding_details: str | None = None
                min_age: int | None = None
                max_age: int | None = None
                target_gender: str | None = None
                target_academic_levels: list | None = None
                is_legitimate: bool = True
                confidence_score: float = 0.99

            monkeypatch.setattr(
                reverification_module.ai_gateway, "verify_and_embed",
                AsyncMock(return_value=(FakeVerification(), [0.0] * 8)),
            )
            outcome = await reverify_opportunity(db, opportunity=opp)

            assert outcome.ai_call_failed is False
            assert outcome.cycle_result.verification_status == VerificationDecisionStatus.VERIFIED
            assert len(outcome.cycle_result.recorded) == 1  # a real deadline shift is HIGH, not IGNORE

            reloaded = await db.get(Opportunity, opp_id)
            assert reloaded.deadline is not None
            assert abs((reloaded.deadline.replace(tzinfo=dt.timezone.utc) - new_deadline).total_seconds()) < 1

    @pytest.mark.asyncio
    async def test_reprocessing_after_the_fix_does_not_redetect_the_same_change(self, db_session_factory, monkeypatch):
        """Direct regression test for the "fires every cycle forever" bug:
        once the opportunity row is updated, a second re-verification
        cycle against the SAME AI answer must find nothing new."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(
                db, source_id=source_id, eligibility_criteria="Open to Egypt", raw_extracted_data='{"title": "x"}',
            )

            from dataclasses import dataclass
            from unittest.mock import AsyncMock

            import app.services.opportunity_reverification as reverification_module

            @dataclass
            class FakeVerification:
                deadline: str | None = None
                eligibility_criteria: str | None = "Open to Egypt and Jordan"
                funding_details: str | None = None
                min_age: int | None = None
                max_age: int | None = None
                target_gender: str | None = None
                target_academic_levels: list | None = None
                is_legitimate: bool = True
                confidence_score: float = 0.99

            monkeypatch.setattr(
                reverification_module.ai_gateway, "verify_and_embed",
                AsyncMock(return_value=(FakeVerification(), [0.0] * 8)),
            )

            opp = await db.get(Opportunity, opp_id)
            first = await reverify_opportunity(db, opportunity=opp)
            assert len(first.cycle_result.recorded) == 1

            opp_again = await db.get(Opportunity, opp_id)
            second = await reverify_opportunity(db, opportunity=opp_again)
            assert second.cycle_result.detected == []  # already applied -- nothing left to detect
            assert second.cycle_result.recorded == []

            result = await db.execute(
                select(OpportunityChangeHistory).where(OpportunityChangeHistory.opportunity_id == opp_id)
            )
            assert len(result.scalars().all()) == 1  # not two

    @pytest.mark.asyncio
    async def test_hold_verification_still_writes_nothing_to_the_opportunity_row(self, db_session_factory, monkeypatch):
        """The fix must not weaken Part D/R: a HOLD (low-confidence)
        candidate must still leave the opportunity row completely
        untouched, exactly as before."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(
                db, source_id=source_id, eligibility_criteria="Original text", raw_extracted_data='{"title": "x"}',
            )
            opp = await db.get(Opportunity, opp_id)

            from dataclasses import dataclass
            from unittest.mock import AsyncMock

            import app.services.opportunity_reverification as reverification_module

            @dataclass
            class FakeVerification:
                deadline: str | None = None
                eligibility_criteria: str | None = "Wildly different text"
                funding_details: str | None = None
                min_age: int | None = None
                max_age: int | None = None
                target_gender: str | None = None
                target_academic_levels: list | None = None
                is_legitimate: bool = True
                confidence_score: float = 0.1  # below threshold -> HOLD

            monkeypatch.setattr(
                reverification_module.ai_gateway, "verify_and_embed",
                AsyncMock(return_value=(FakeVerification(), [0.0] * 8)),
            )
            await reverify_opportunity(db, opportunity=opp)

            reloaded = await db.get(Opportunity, opp_id)
            assert reloaded.eligibility_criteria == "Original text"


class TestNotificationPreferenceRaceSafety:
    """Audit finding (P1/P2): `get_or_create_preferences` had no guard
    against a concurrent first-time insert for the same user
    (`user_id` is this table's own primary key) -- an `IntegrityError`
    would propagate uncaught, and (since this runs deep inside the same
    session/transaction as an already-flushed, real `OpportunityChangeHistory`
    row) a naive `db.rollback()` fix would have discarded that
    unrelated, already-verified change record too.

    A genuine two-connection race is not reproducible against this
    fixture's single shared in-memory SQLite connection (StaticPool), so
    the race is simulated deterministically: the "winning" row is
    committed for real FIRST, then `AsyncSession.get`'s first call is
    monkeypatched to (falsely) report "not found yet" -- reproducing the
    exact stale-read window a real race would create -- so
    `get_or_create_preferences` proceeds to attempt its own insert,
    which then hits a REAL `IntegrityError` against the genuinely
    already-committed row (not something this test's own rollback could
    undo), exercising the actual production recovery path."""

    @pytest.mark.asyncio
    async def test_simulated_race_is_handled_without_raising_and_returns_existing_row(self, db_session_factory, monkeypatch):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            await db.commit()

            # The "other worker" already really committed its row.
            await db.execute(
                text(
                    "INSERT INTO user_notification_preferences "
                    "(user_id, opportunity_changes, deadline_alerts, application_alerts) "
                    "VALUES (:uid, 1, 1, 1)"
                ),
                {"uid": user_id.hex},
            )
            await db.commit()

            from sqlalchemy.ext.asyncio import AsyncSession

            real_get = AsyncSession.get
            state = {"calls": 0}

            async def stale_get(self, *args, **kwargs):
                state["calls"] += 1
                if state["calls"] == 1:
                    return None  # simulate the stale read that missed the real commit above
                return await real_get(self, *args, **kwargs)

            monkeypatch.setattr(AsyncSession, "get", stale_get)

            result = await notification_preferences.get_or_create_preferences(db, user_id)
            assert result.user_id == user_id
            assert state["calls"] >= 2  # confirms the race/refetch branch actually ran

    @pytest.mark.asyncio
    async def test_race_does_not_roll_back_an_unrelated_pending_change_history_row(self, db_session_factory, monkeypatch):
        """The savepoint-scoped fix must not discard other, unrelated
        pending work in the same session/transaction (the failure mode a
        bare `db.rollback()` fix would have had)."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            user_id = await _make_user(db)
            opp_id = await _make_opportunity(db, source_id=source_id, eligibility_criteria="Original")
            await db.commit()

            await db.execute(
                text(
                    "INSERT INTO user_notification_preferences "
                    "(user_id, opportunity_changes, deadline_alerts, application_alerts) "
                    "VALUES (:uid, 1, 1, 1)"
                ),
                {"uid": user_id.hex},
            )
            await db.commit()

            from app.services.change_history_service import record_change_if_meaningful

            # A real, unrelated, still-uncommitted (flushed only) history
            # row already sitting in this same session -- exactly the
            # state `create_change_notifications_for_opportunity` leaves
            # things in when it calls `get_or_create_preferences`.
            row = await record_change_if_meaningful(
                db, opportunity_id=opp_id, field_name="eligibility_criteria",
                old_value="Original", new_value="Changed", change_reason="test",
            )
            assert row is not None

            from sqlalchemy.ext.asyncio import AsyncSession

            real_get = AsyncSession.get
            state = {"calls": 0}

            async def stale_get(self, *args, **kwargs):
                state["calls"] += 1
                if state["calls"] == 1:
                    return None
                return await real_get(self, *args, **kwargs)

            monkeypatch.setattr(AsyncSession, "get", stale_get)

            await notification_preferences.get_or_create_preferences(db, user_id)

            # The earlier, unrelated history row must still be present in
            # this same session (not rolled back by the savepoint above).
            still_there = await db.get(OpportunityChangeHistory, row.id)
            assert still_there is not None


# ============================================================
# SCHEDULING
# ============================================================


class TestScheduling:
    @pytest.mark.asyncio
    async def test_active_application_opportunity_is_highest_priority(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            user_id = await _make_user(db)
            opp_low = await _make_opportunity(db, source_id=source_id, title="Low priority")
            opp_high = await _make_opportunity(db, source_id=source_id, title="High priority")
            await _make_tracker(db, user_id=user_id, opportunity_id=opp_high, status="interested")

            ids = await find_opportunities_to_monitor(db, limit=10)
            assert ids[0] == opp_high

    @pytest.mark.asyncio
    async def test_bookmarked_outranks_neither(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            user_id = await _make_user(db)
            opp_neither = await _make_opportunity(db, source_id=source_id, title="Neither")
            opp_saved = await _make_opportunity(db, source_id=source_id, title="Saved")
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_saved)

            ids = await find_opportunities_to_monitor(db, limit=10)
            assert ids.index(opp_saved) < ids.index(opp_neither)

    @pytest.mark.asyncio
    async def test_unpublished_and_unrelated_opportunity_is_excluded(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, review_status="pending", title="Not published")
            ids = await find_opportunities_to_monitor(db, limit=10)
            assert ids == []

    @pytest.mark.asyncio
    async def test_closed_unrelated_opportunity_is_excluded(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, lifecycle_state="closed", title="Closed, no relationship")
            ids = await find_opportunities_to_monitor(db, limit=10)
            assert ids == []

    @pytest.mark.asyncio
    async def test_never_checked_outranks_recently_checked_within_same_tier(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_recent = await _make_opportunity(
                db, source_id=source_id, title="Recently checked",
                monitoring_last_checked_at=_now().isoformat(),
            )
            opp_never = await _make_opportunity(db, source_id=source_id, title="Never checked")

            ids = await find_opportunities_to_monitor(db, limit=10)
            assert ids.index(opp_never) < ids.index(opp_recent)

    @pytest.mark.asyncio
    async def test_limit_is_respected(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            user_id = await _make_user(db)
            for i in range(5):
                opp_id = await _make_opportunity(db, source_id=source_id, title=f"Opp {i}")
                await _make_tracker(db, user_id=user_id, opportunity_id=opp_id, status="interested")
            ids = await find_opportunities_to_monitor(db, limit=3)
            assert len(ids) == 3


# ============================================================
# GUIDANCE INTEGRATION
# ============================================================


class TestGuidanceIntegration:
    @pytest.mark.asyncio
    async def test_material_change_on_saved_opportunity_surfaces_in_guidance(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, eligibility_criteria="Open to Egypt")
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_id)

            opp = await db.get(Opportunity, opp_id)
            await process_opportunity_candidate(
                db, opportunity=opp, candidate={"eligibility_criteria": "Open to Egypt, slightly different"},
                source_trust="structured", change_reason="test",
            )

            from app.models.user import User

            # NOTE: in production, build_guidance's caller (the
            # /user-intelligence/guidance endpoint) always fetches the
            # current user fresh, at the very start of its own request
            # session, before any opportunity/bookmark relationship is
            # ever touched in that session. This test instead fetches the
            # Opportunity first (as the monitoring pipeline does), which
            # populates the SQLAlchemy identity map with this same User
            # via the Opportunity.bookmarks -> UserBookmark.user selectin
            # chain -- but that chain does not itself cascade a selectin
            # load of THAT user's own .bookmarks. db.get(User, ...) then
            # returns the identity-map hit as-is, leaving .bookmarks
            # unloaded. An explicit refresh here simulates the fresh,
            # separate-session fetch that always happens on the real
            # request path, rather than masking a genuine production bug.
            user = await db.get(User, user_id)
            await db.refresh(user, ["bookmarks"])
            guidance = await build_guidance(db, user=user, now=_now())
            categories = {item.category for item in guidance.items}
            assert "saved_opportunity_material_change" in categories

    @pytest.mark.asyncio
    async def test_material_change_on_active_application_surfaces_in_guidance(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, funding_details="Full funding")
            await _make_tracker(db, user_id=user_id, opportunity_id=opp_id, status="preparing_docs")

            opp = await db.get(Opportunity, opp_id)
            await process_opportunity_candidate(
                db, opportunity=opp, candidate={"funding_details": "Full funding, slightly reworded"},
                source_trust="structured", change_reason="test",
            )

            from app.models.user import User

            user = await db.get(User, user_id)
            guidance = await build_guidance(db, user=user, now=_now())
            categories = {item.category for item in guidance.items}
            assert "active_application_material_change" in categories

    @pytest.mark.asyncio
    async def test_ignore_severity_change_never_surfaces_in_guidance(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            d = _now()
            opp_id = await _make_opportunity(db, source_id=source_id, deadline=d.isoformat())
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_id)

            opp = await db.get(Opportunity, opp_id)
            result = await process_opportunity_candidate(
                db, opportunity=opp, candidate={"deadline": d}, source_trust="structured", change_reason="test",
            )
            assert result.recorded == []

            from app.models.user import User

            # See note in test_material_change_on_saved_opportunity_surfaces_in_guidance
            # above: this refresh simulates the fresh, separate-session user
            # fetch that always happens on the real /guidance request path.
            user = await db.get(User, user_id)
            await db.refresh(user, ["bookmarks"])
            guidance = await build_guidance(db, user=user, now=_now())
            categories = {item.category for item in guidance.items}
            assert "saved_opportunity_material_change" not in categories


# ============================================================
# USER ISOLATION
# ============================================================


class TestUserIsolation:
    @pytest.mark.asyncio
    async def test_notification_never_leaks_to_unrelated_user(self, db_session_factory):
        async with db_session_factory() as db:
            related_user = await _make_user(db)
            unrelated_user = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, lifecycle_state="open")
            await _make_bookmark(db, user_id=related_user, opportunity_id=opp_id)

            opp = await db.get(Opportunity, opp_id)
            await process_opportunity_candidate(
                db, opportunity=opp, candidate={"lifecycle_state": "closed"},
                source_trust="structured", change_reason="test",
            )

            unrelated_notes = (
                await db.execute(select(Notification).where(Notification.user_id == unrelated_user))
            ).scalars().all()
            assert unrelated_notes == []

    @pytest.mark.asyncio
    async def test_guidance_never_leaks_another_users_saved_opportunity_change(self, db_session_factory):
        async with db_session_factory() as db:
            user_a = await _make_user(db)
            user_b = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, eligibility_criteria="Open to Egypt")
            await _make_bookmark(db, user_id=user_a, opportunity_id=opp_id)

            opp = await db.get(Opportunity, opp_id)
            await process_opportunity_candidate(
                db, opportunity=opp, candidate={"eligibility_criteria": "Open to Egypt, edited"},
                source_trust="structured", change_reason="test",
            )

            from app.models.user import User

            user_b_obj = await db.get(User, user_b)
            guidance = await build_guidance(db, user=user_b_obj, now=_now())
            categories = {item.category for item in guidance.items}
            assert "saved_opportunity_material_change" not in categories

    @pytest.mark.asyncio
    async def test_notification_preferences_are_per_user(self, db_session_factory):
        async with db_session_factory() as db:
            user_a = await _make_user(db)
            user_b = await _make_user(db)
            pref_a = await notification_preferences.get_or_create_preferences(db, user_a)
            pref_a.opportunity_changes = False
            db.add(pref_a)
            await db.commit()

            assert await notification_preferences.is_opted_in(db, user_a, "opportunity_changes") is False
            assert await notification_preferences.is_opted_in(db, user_b, "opportunity_changes") is True


# ============================================================
# PERFORMANCE / CORRECTNESS
# ============================================================


class TestPerformanceAndCorrectness:
    @pytest.mark.asyncio
    async def test_multiple_changed_opportunities_each_processed_independently(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp1 = await _make_opportunity(db, source_id=source_id, lifecycle_state="open", title="Opp 1")
            opp2 = await _make_opportunity(db, source_id=source_id, lifecycle_state="open", title="Opp 2")
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp1)
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp2)

            for opp_id in (opp1, opp2):
                opp = await db.get(Opportunity, opp_id)
                await process_opportunity_candidate(
                    db, opportunity=opp, candidate={"lifecycle_state": "closed"},
                    source_trust="structured", change_reason="test",
                )

            notes = (await db.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all()
            assert len(notes) == 2

    @pytest.mark.asyncio
    async def test_repeated_execution_with_no_new_data_is_deterministic_and_idempotent(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            deadline = _now()
            opp_id = await _make_opportunity(db, source_id=source_id, deadline=deadline.isoformat())

            for _ in range(3):
                opp = await db.get(Opportunity, opp_id)
                result = await process_opportunity_candidate(
                    db, opportunity=opp, candidate={"deadline": deadline}, source_trust="structured", change_reason="test",
                )
                assert result.recorded == []
                assert result.notifications_created == 0

            history = (await db.execute(select(OpportunityChangeHistory).where(OpportunityChangeHistory.opportunity_id == opp_id))).scalars().all()
            assert history == []

    @pytest.mark.asyncio
    async def test_empty_candidate_never_creates_recorded_change_or_notification(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            opp = await db.get(Opportunity, opp_id)
            result = await process_opportunity_candidate(
                db, opportunity=opp, candidate={}, source_trust="structured", change_reason="test",
            )
            assert result.detected == []
            assert result.recorded == []
            assert result.notifications_created == 0
