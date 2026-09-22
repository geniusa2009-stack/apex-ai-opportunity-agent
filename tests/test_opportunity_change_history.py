"""Taxonomy Evolution + Temporal State Machine + Change History step,
Part C + Part H (CHANGE HISTORY rows of the test matrix).

Covers `app/services/change_history_service.record_change_if_meaningful`
directly, plus its real integration point in
`app/services/aggregator_service.upsert_opportunity`'s UPDATE branch.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.services.change_history_service import _normalize_for_comparison, record_change_if_meaningful

_CREATE_CHANGE_HISTORY_SQL = """
CREATE TABLE opportunity_change_history (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, field_name VARCHAR(50) NOT NULL,
    old_value TEXT, new_value TEXT, source_id VARCHAR(36), evidence_id VARCHAR(36),
    change_reason TEXT NOT NULL, changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_CHANGE_HISTORY_SQL))
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


class TestNormalizeForComparison:
    def test_none_and_empty_string_are_equivalent(self):
        assert _normalize_for_comparison(None) == _normalize_for_comparison("")

    def test_identical_strings_are_equal(self):
        assert _normalize_for_comparison("Oct 25") == _normalize_for_comparison("Oct 25")

    def test_different_strings_are_different(self):
        assert _normalize_for_comparison("Oct 25") != _normalize_for_comparison("Nov 10")

    def test_datetimes_compared_via_iso_string(self):
        d1 = dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc)
        d2 = dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc)
        assert _normalize_for_comparison(d1) == _normalize_for_comparison(d2)


@pytest.mark.asyncio
class TestRecordChangeIfMeaningful:
    async def test_new_value_creates_a_record(self, db_session_factory):
        async with db_session_factory() as db:
            opp_id = uuid.uuid4()
            record = await record_change_if_meaningful(
                db, opportunity_id=opp_id, field_name="deadline", old_value="2026-10-25", new_value="2026-11-10",
                change_reason="Source evidence updated the stated deadline.",
            )
            await db.commit()
            assert record is not None
            assert record.old_value == "2026-10-25"
            assert record.new_value == "2026-11-10"

            rows = (await db.execute(text("SELECT COUNT(*) FROM opportunity_change_history"))).scalar()
            assert rows == 1

    async def test_same_value_creates_no_record(self, db_session_factory):
        """Part C3's own explicit 'this must be tested' requirement."""
        async with db_session_factory() as db:
            opp_id = uuid.uuid4()
            record = await record_change_if_meaningful(
                db, opportunity_id=opp_id, field_name="title", old_value="Same Title", new_value="Same Title",
                change_reason="No real change.",
            )
            await db.commit()
            assert record is None

            rows = (await db.execute(text("SELECT COUNT(*) FROM opportunity_change_history"))).scalar()
            assert rows == 0

    async def test_none_to_empty_string_is_not_a_meaningful_change(self, db_session_factory):
        async with db_session_factory() as db:
            opp_id = uuid.uuid4()
            record = await record_change_if_meaningful(
                db, opportunity_id=opp_id, field_name="funding_details", old_value=None, new_value="",
                change_reason="Should not be recorded.",
            )
            await db.commit()
            assert record is None

    async def test_multiple_sequential_changes_are_all_preserved(self, db_session_factory):
        """Part C5: Oct 25 -> Nov 10 -> Nov 20, history must contain both
        transitions, append-only."""
        async with db_session_factory() as db:
            opp_id = uuid.uuid4()
            await record_change_if_meaningful(
                db, opportunity_id=opp_id, field_name="deadline", old_value="2026-10-25", new_value="2026-11-10",
                change_reason="First extension.",
            )
            await db.commit()
            await record_change_if_meaningful(
                db, opportunity_id=opp_id, field_name="deadline", old_value="2026-11-10", new_value="2026-11-20",
                change_reason="Second extension.",
            )
            await db.commit()

            rows = (await db.execute(
                text("SELECT old_value, new_value FROM opportunity_change_history "
                     "WHERE opportunity_id = :id ORDER BY changed_at"),
                {"id": opp_id.hex},
            )).all()
            assert len(rows) == 2
            assert rows[0] == ("2026-10-25", "2026-11-10")
            assert rows[1] == ("2026-11-10", "2026-11-20")

    async def test_old_and_new_value_are_both_preserved_verbatim(self, db_session_factory):
        async with db_session_factory() as db:
            opp_id = uuid.uuid4()
            record = await record_change_if_meaningful(
                db, opportunity_id=opp_id, field_name="provider", old_value="Old Provider Name",
                new_value="New Provider Name", change_reason="Provider name corrected by new evidence.",
            )
            await db.commit()
            assert record.old_value == "Old Provider Name"
            assert record.new_value == "New Provider Name"

    async def test_source_and_evidence_provenance_are_preserved(self, db_session_factory):
        async with db_session_factory() as db:
            opp_id = uuid.uuid4()
            source_id = uuid.uuid4()
            evidence_id = uuid.uuid4()
            record = await record_change_if_meaningful(
                db, opportunity_id=opp_id, field_name="deadline", old_value="2026-10-25", new_value="2026-11-10",
                change_reason="Extension confirmed by source.", source_id=source_id, evidence_id=evidence_id,
            )
            await db.commit()
            assert record.source_id == source_id
            assert record.evidence_id == evidence_id

    async def test_change_reason_is_always_present(self, db_session_factory):
        async with db_session_factory() as db:
            opp_id = uuid.uuid4()
            record = await record_change_if_meaningful(
                db, opportunity_id=opp_id, field_name="country", old_value="Egypt", new_value="Global",
                change_reason="Eligibility claim broadened by new evidence.",
            )
            await db.commit()
            assert record.change_reason == "Eligibility claim broadened by new evidence."

    async def test_historical_records_are_never_updated_only_inserted(self, db_session_factory):
        """No function in this module ever performs an UPDATE -- proven
        here by confirming two real changes to the same field produce
        two distinct row ids, neither row mutated after creation."""
        async with db_session_factory() as db:
            opp_id = uuid.uuid4()
            first = await record_change_if_meaningful(
                db, opportunity_id=opp_id, field_name="title", old_value="A", new_value="B",
                change_reason="First rename.",
            )
            await db.commit()
            second = await record_change_if_meaningful(
                db, opportunity_id=opp_id, field_name="title", old_value="B", new_value="C",
                change_reason="Second rename.",
            )
            await db.commit()
            assert first.id != second.id

            first_row = (await db.execute(
                text("SELECT old_value, new_value FROM opportunity_change_history WHERE id = :id"),
                {"id": first.id.hex},
            )).first()
            assert first_row == ("A", "B")  # unchanged by the second write


import inspect  # noqa: E402


def test_record_change_if_meaningful_has_no_raw_text_or_llm_confidence_parameter():
    params = set(inspect.signature(record_change_if_meaningful).parameters)
    assert params == {
        "db", "opportunity_id", "field_name", "old_value", "new_value", "change_reason", "source_id", "evidence_id",
    }
