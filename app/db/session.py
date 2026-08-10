from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# `pool_pre_ping=True` was already in place before the Supabase
# integration step and remains the right call for a remote/serverless-
# style Postgres host -- it issues a cheap `SELECT 1` before handing out
# a pooled connection, so a connection Supabase silently closed (e.g.
# after an idle timeout) gets transparently replaced instead of
# surfacing as a confusing mid-request `OperationalError`.
#
# Known Supabase-specific gotcha, NOT currently worked around here: if
# `DATABASE_URL` points at Supabase's pgbouncer transaction-mode pooler
# (port 6543) rather than the direct connection (port 5432), asyncpg's
# default server-side prepared-statement caching breaks -- pgbouncer in
# transaction mode can hand the same logical connection's statements to
# different physical backend connections, and a prepared statement
# doesn't necessarily exist on whichever backend picks up the next
# query. This surfaces as intermittent
# `prepared statement "..." does not exist` errors under load, not on
# every query, which makes it easy to miss in casual testing. If/when
# this app is actually pointed at the 6543 pooler port, the fix is to
# add `connect_args={"statement_cache_size": 0}` (asyncpg's own knob for
# this) to `create_async_engine` below -- left as a note rather than
# applied automatically, since forcing it off unconditionally is a
# measurable performance cost that only exists to work around a
# very-specific mode this app might not even be deployed in (using the
# direct 5432 connection, or Supabase's newer "Supavisor" session-mode
# pooler, doesn't hit this at all).
engine = create_async_engine(
    settings.database_url,
    echo=settings.environment == "development",
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session per request."""
    async with AsyncSessionLocal() as session:
        yield session
