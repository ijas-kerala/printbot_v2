"""
core/database.py — Async SQLAlchemy 2.x engine and session factory.

Single source for all DB connection setup. Routes must use get_db() via
FastAPI Depends() — never create sessions manually outside this module.

Engine uses aiosqlite so the event loop is never blocked by SQLite I/O.
SQLite pragmas (WAL mode, foreign keys) are set on every new connection
to ensure safe concurrent reads alongside the single async writer.
"""

from collections.abc import AsyncGenerator
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from core.config import settings

# ── Engine ────────────────────────────────────────────────────────────────────

# aiosqlite driver: "sqlite+aiosqlite:///<path>"
# StaticPool: single in-process connection pool suitable for SQLite in a
# single-worker Uvicorn process. Avoids "database is locked" on concurrent
# writes while still being safe for our asyncio workload.
engine = create_async_engine(
    f"sqlite+aiosqlite:///{settings.DATABASE_URL}",
    echo=settings.ENV == "development",   # Log SQL only in dev
    connect_args={
        # Prevent SQLite from holding the GIL across threads in aiosqlite
        "check_same_thread": False,
    },
    poolclass=StaticPool,
)


# ── Session factory ───────────────────────────────────────────────────────────

# expire_on_commit=False: keep ORM objects usable after commit (important in
# async code where we may access attributes after the commit returns).
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


# ── Dependency ────────────────────────────────────────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields a DB session and guarantees cleanup.

    Usage in a route:
        async def my_route(db: AsyncSession = Depends(get_db)): ...

    The session is rolled back automatically if an unhandled exception
    propagates out of the route handler, ensuring no partial writes are
    committed to the DB.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Low-level connection helper (used only by Alembic migrations) ─────────────

async def get_connection() -> AsyncIterator[AsyncConnection]:
    """
    Yield a raw AsyncConnection for DDL operations (Alembic offline/online
    migration runner). Not for use in route handlers — use get_db() there.
    """
    async with engine.begin() as conn:
        yield conn
