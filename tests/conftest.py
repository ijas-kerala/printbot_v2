"""
tests/conftest.py — Shared pytest fixtures for the PrintBot v3 test suite.

Provides:
  async_db   — In-memory async SQLite session; schema created fresh per test.
               All test modules that touch the DB must use this fixture.
  sample_job — A PrintJob in PAYMENT_PENDING status, already flushed to async_db.

Notes:
  - asyncio_mode = "auto" is set in pytest.ini so every async test function
    is awaited automatically without needing @pytest.mark.asyncio decorators.
    If pytest-asyncio is <0.21, each test still needs @pytest.mark.asyncio.
  - The in-memory SQLite engine uses StaticPool so the same connection is
    reused across the session; without it each new connection would open a
    different in-memory database (empty).
"""

from __future__ import annotations

import secrets
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from core.models import Base, JobStatus, PrintJob


# ── Engine shared across all fixtures ─────────────────────────────────────────

@pytest_asyncio.fixture
async def async_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield a fresh async DB session backed by an in-memory SQLite database.

    The schema is created (and dropped) around each test so every test starts
    with a clean state.  StaticPool is mandatory for in-memory SQLite — without
    it, SQLAlchemy would open a new connection (and a new empty DB) for every
    operation.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",   # in-memory
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    async with session_factory() as session:
        yield session

    # Tear down schema after the test
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture
async def sample_job(async_db: AsyncSession) -> PrintJob:
    """
    Insert and return a PrintJob in PAYMENT_PENDING status.

    Suitable as a starting point for payment idempotency tests that need a
    job that has reached the payment gate but not yet been paid.
    """
    job = PrintJob(
        id="test-job-0001-0000-0000-000000000001",
        session_token=secrets.token_hex(16),
        status=JobStatus.PAYMENT_PENDING,
        total_cost=10.0,
        razorpay_order_id="order_test123",
    )
    async_db.add(job)
    await async_db.flush()
    return job
