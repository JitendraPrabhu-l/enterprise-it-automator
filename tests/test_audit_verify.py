"""Tests for GET /audit/verify — admin-only, on-demand hash-chain integrity
check over the audit log (app/db/audit.py).
"""

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import session as db_session_module
from app.db.audit import append_audit_log
from app.db.models import ApiClient, ApiClientRole, AuditLog


@pytest.fixture
async def app_client(monkeypatch, tmp_path):
    db_path = tmp_path / "audit_verify_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("API_KEY", "admin-bootstrap-key")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    import app.api.main as main_module

    await db_session_module.init_db()
    await main_module._ensure_bootstrap_admin_client()

    async with db_session_module.session_scope() as session:
        session.add(ApiClient(name="standard-caller", role=ApiClientRole.STANDARD, key="standard-key"))
        await append_audit_log(
            session, ticket_id=None, actor="test", tool_name="disable_user",
            tool_args={"username": "jsmith"}, result="ok", success=True,
        )

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_verify_rejects_standard_client(app_client):
    resp = await app_client.get("/audit/verify", headers={"X-API-Key": "standard-key"})
    assert resp.status_code == 403


async def test_verify_rejects_missing_key(app_client):
    resp = await app_client.get("/audit/verify")
    assert resp.status_code == 401


async def test_verify_reports_ok_for_untampered_chain(app_client):
    resp = await app_client.get("/audit/verify", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


async def test_verify_reports_tampering_and_logs_a_security_event(app_client):
    async with db_session_module.session_scope() as session:
        row = (await session.scalars(select(AuditLog).where(AuditLog.tool_name == "disable_user"))).first()
        row.result = "tampered"

    resp = await app_client.get("/audit/verify", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "row" in body["detail"]

    async with db_session_module.session_scope() as session:
        events = (
            await session.scalars(
                select(AuditLog).where(AuditLog.tool_name == "audit_chain_verification_failed")
            )
        ).all()
    assert len(events) == 1
