"""
alembic/env.py — Alembic migration environment for PrintBot v3.

Configured for SQLAlchemy 2.x async (aiosqlite).  Alembic's own migration
runner is synchronous, so we use run_async_engine_migrations() via
asyncio.run() to bridge the gap — this is the officially recommended
pattern for async Alembic environments.

Both online (live DB) and offline (SQL script output) modes are supported.
"""

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# ── Make project root importable ──────────────────────────────────────────────
# Alembic is run from the project root, but the Python path may not include it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import settings
from core.models import Base  # noqa: E402 — must come after sys.path fix

# ── Alembic config object ─────────────────────────────────────────────────────
config = context.config

# Configure Python logging from alembic.ini [loggers] section
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata object Alembic uses for --autogenerate support.
# Points to the same Base that all our models inherit from.
target_metadata = Base.metadata

# Override the DB URL with the value from core/config.py so there is a single
# source of truth.  The alembic.ini sqlalchemy.url is only a fallback.
_DB_URL = f"sqlite+aiosqlite:///{settings.DATABASE_URL}"
config.set_main_option("sqlalchemy.url", _DB_URL)


# ── Offline mode ──────────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    """
    Generate migration SQL without connecting to the database.

    Useful for reviewing what a migration will do or for applying it
    manually on a production system.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite does not support transactional DDL the same way Postgres does,
        # but we keep render_as_batch=True so Alembic uses batch operations for
        # ALTER TABLE emulation on SQLite (required for column drops/renames).
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ── Online mode ───────────────────────────────────────────────────────────────

def do_run_migrations(connection: Connection) -> None:
    """Run migrations against a live sync connection (called from async wrapper)."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # IMPORTANT: render_as_batch=True is mandatory for SQLite because it
        # does not support ALTER COLUMN / DROP COLUMN natively. Alembic
        # emulates them by recreating the table in a batch operation.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and hand a sync connection to Alembic."""
    # NullPool prevents the engine from caching connections — we only need a
    # short-lived connection for the migration run.
    connectable = create_async_engine(
        _DB_URL,
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        # run_sync() lets us execute synchronous Alembic context code inside
        # the async engine without spawning a thread.
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations (called by Alembic CLI)."""
    asyncio.run(run_async_migrations())


# ── Dispatch ──────────────────────────────────────────────────────────────────

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
