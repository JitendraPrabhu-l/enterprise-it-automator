"""Tests for the audit-log hash chain (app/db/audit.py) — sequential writes
form a valid chain, and both an in-place edit and a trailing-row deletion
are detected by verify_audit_chain().
"""

import pytest

from app.config import get_settings
from app.db import session as db_session_module
from app.db.audit import _GENESIS_HASH, append_audit_log, verify_audit_chain
from app.db.models import AuditLog
from app.db.session import session_scope


@pytest.fixture
async def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "audit_chain_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    await db_session_module.init_db()
    yield
    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_first_entry_chains_to_genesis(isolated_db):
    async with session_scope() as session:
        row = await append_audit_log(
            session, ticket_id=None, actor="test", tool_name="t", tool_args={}, result="r", success=True
        )
        assert row.prev_hash == _GENESIS_HASH
        assert row.entry_hash is not None


async def test_sequential_writes_form_a_valid_chain(isolated_db):
    async with session_scope() as session:
        for i in range(5):
            await append_audit_log(
                session, ticket_id=None, actor="test", tool_name=f"tool_{i}",
                tool_args={"i": i}, result=f"result {i}", success=True,
            )

    async with session_scope() as session:
        ok, detail = await verify_audit_chain(session)
    assert ok, detail


async def test_second_entry_chains_to_first_entrys_hash(isolated_db):
    async with session_scope() as session:
        first = await append_audit_log(
            session, ticket_id=None, actor="a", tool_name="t1", tool_args={}, result="r1", success=True
        )
    async with session_scope() as session:
        second = await append_audit_log(
            session, ticket_id=None, actor="a", tool_name="t2", tool_args={}, result="r2", success=True
        )
    assert second.prev_hash == first.entry_hash


async def test_editing_a_row_breaks_verification(isolated_db):
    async with session_scope() as session:
        for i in range(3):
            await append_audit_log(
                session, ticket_id=None, actor="test", tool_name=f"tool_{i}",
                tool_args={}, result=f"result {i}", success=True,
            )

    async with session_scope() as session:
        ok, _ = await verify_audit_chain(session)
    assert ok

    # Tamper: directly mutate a stored row's result, bypassing append_audit_log.
    async with session_scope() as session:
        row = await session.get(AuditLog, 2)
        row.result = "tampered result"

    async with session_scope() as session:
        ok, detail = await verify_audit_chain(session)
    assert not ok
    assert "row 2" in detail


async def test_deleting_the_last_row_breaks_verification(isolated_db):
    async with session_scope() as session:
        for i in range(3):
            await append_audit_log(
                session, ticket_id=None, actor="test", tool_name=f"tool_{i}",
                tool_args={}, result=f"result {i}", success=True,
            )

    async with session_scope() as session:
        last = await session.get(AuditLog, 3)
        await session.delete(last)

    async with session_scope() as session:
        ok, detail = await verify_audit_chain(session)
    assert not ok
    assert "deleted" in detail


async def test_empty_audit_log_verifies_ok(isolated_db):
    async with session_scope() as session:
        ok, detail = await verify_audit_chain(session)
    assert ok, detail


async def test_concurrent_appends_do_not_fork_the_chain(isolated_db):
    """Two 'concurrent' append_audit_log calls inside the SAME transaction
    (simulating two rows written by one request, e.g. a rejection audit
    followed by a real action) must chain sequentially, not both read the
    same prev_hash — with_for_update's lock is a no-op on SQLite (single
    writer already), but the sequential-within-one-session behavior is
    exactly what this test pins regardless of backend.
    """
    async with session_scope() as session:
        first = await append_audit_log(
            session, ticket_id=None, actor="a", tool_name="t1", tool_args={}, result="r1", success=True
        )
        second = await append_audit_log(
            session, ticket_id=None, actor="a", tool_name="t2", tool_args={}, result="r2", success=True
        )
    assert second.prev_hash == first.entry_hash
    assert second.entry_hash != first.entry_hash

    async with session_scope() as session:
        ok, detail = await verify_audit_chain(session)
    assert ok, detail
