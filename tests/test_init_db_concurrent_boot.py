"""Regression test for a real bug found via live docker-compose/Render
verification against Postgres: init_db() previously let a Postgres
IntegrityError (unique-violation on pg_type, from CREATE TYPE racing across
this app's 2 concurrently-booting gunicorn workers) propagate and crash
worker startup entirely.

Reproduced live against a real Neon database: two init_db() calls fired
concurrently against a genuinely empty schema raced on
`CREATE TYPE userstatus AS ENUM (...)`, and the loser's IntegrityError took
gunicorn's whole master process down ("Worker failed to boot"). SQLite has
no equivalent race (no ENUM concept at the DB level), so this was invisible
under local/SQLite-only testing.

A real Postgres connection is the only way to reproduce the actual race
(SQLite doesn't have ENUM types to collide on), so this test instead pins
the narrower, real-Postgres-independent contract: init_db() must swallow
IntegrityError from the create_all call, not propagate it.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.session import init_db


async def test_init_db_swallows_integrity_error_from_concurrent_create_type():
    fake_conn = AsyncMock()
    fake_conn.run_sync.side_effect = IntegrityError("CREATE TYPE ...", {}, Exception("dup"))

    fake_begin_cm = AsyncMock()
    fake_begin_cm.__aenter__.return_value = fake_conn
    fake_begin_cm.__aexit__.return_value = False

    fake_engine = MagicMock()
    fake_engine.begin = MagicMock(return_value=fake_begin_cm)

    with patch("app.db.session.get_engine", return_value=fake_engine):
        await init_db()  # must not raise


async def test_init_db_still_propagates_other_errors(monkeypatch):
    """The swallow is scoped to IntegrityError specifically — a genuine
    connectivity failure (e.g. the database is unreachable) must still
    surface, not be silently swallowed alongside the benign race."""
    fake_conn = AsyncMock()
    fake_conn.run_sync.side_effect = ConnectionError("could not connect")

    fake_begin_cm = AsyncMock()
    fake_begin_cm.__aenter__.return_value = fake_conn
    fake_begin_cm.__aexit__.return_value = False

    fake_engine = MagicMock()
    fake_engine.begin = MagicMock(return_value=fake_begin_cm)

    with patch("app.db.session.get_engine", return_value=fake_engine):
        with pytest.raises(ConnectionError):
            await init_db()
