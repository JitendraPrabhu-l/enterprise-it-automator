"""Tests for app/api/security_audit.py and its call sites in app/api/auth.py
— failed auth attempts (invalid API key, invalid reviewer token, rejected
OIDC token) must land as AuditLog rows, using the same table/query surface
as tool-invocation audit entries.
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.auth import require_api_client, require_reviewer_token
from app.api.security_audit import record_security_event
from app.config import get_settings
from app.db.models import AuditLog


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def isolated_db(monkeypatch, tmp_path):
    from app.db import session as db_session_module

    db_path = tmp_path / "security_audit_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    await db_session_module.init_db()
    yield db_session_module
    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_record_security_event_writes_audit_row(isolated_db):
    await record_security_event(actor="auth", event="invalid_api_key", detail="probe")

    async with isolated_db.session_scope() as session:
        rows = (await session.scalars(select(AuditLog).where(AuditLog.actor == "auth"))).all()
    assert len(rows) == 1
    assert rows[0].tool_name == "invalid_api_key"
    assert rows[0].ticket_id is None
    assert rows[0].success is False


async def test_invalid_api_key_is_audited(monkeypatch, isolated_db):
    monkeypatch.setenv("API_KEY", "secret123")
    get_settings.cache_clear()

    with pytest.raises(HTTPException):
        await require_api_client(x_api_key="wrong")

    async with isolated_db.session_scope() as session:
        rows = (
            await session.scalars(select(AuditLog).where(AuditLog.tool_name == "invalid_api_key"))
        ).all()
    assert len(rows) == 1


async def test_invalid_reviewer_token_is_audited(isolated_db):
    with pytest.raises(HTTPException):
        await require_reviewer_token(x_reviewer_token="not-a-real-token")

    async with isolated_db.session_scope() as session:
        rows = (
            await session.scalars(
                select(AuditLog).where(AuditLog.tool_name == "invalid_reviewer_token")
            )
        ).all()
    assert len(rows) == 1


async def test_missing_header_is_not_audited(isolated_db):
    """A caller who simply sends no header at all isn't a credential-guessing
    attempt — only genuinely wrong/invalid credentials get logged, to keep
    the security-event log meaningful rather than noisy."""
    with pytest.raises(HTTPException):
        await require_reviewer_token(x_reviewer_token=None)

    async with isolated_db.session_scope() as session:
        rows = (await session.scalars(select(AuditLog))).all()
    assert rows == []
