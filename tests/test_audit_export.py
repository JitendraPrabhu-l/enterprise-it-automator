"""Tests for GET /audit/export — admin-only JSONL/CSV export of the full
audit log (tool invocations + security events), for compliance/SIEM
ingestion.
"""

import json

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import session as db_session_module
from app.db.models import ApiClient, ApiClientRole, AuditLog


@pytest.fixture
async def app_client(monkeypatch, tmp_path):
    db_path = tmp_path / "audit_export_test.db"
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
        session.add(
            AuditLog(
                ticket_id=None, actor="test", tool_name="disable_user",
                tool_args={"username": "jsmith"}, result="ok", success=True,
            )
        )

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_export_rejects_standard_client(app_client):
    resp = await app_client.get("/audit/export", headers={"X-API-Key": "standard-key"})
    assert resp.status_code == 403


async def test_export_rejects_missing_key(app_client):
    resp = await app_client.get("/audit/export")
    assert resp.status_code == 401


async def test_export_rejects_bad_format(app_client):
    resp = await app_client.get(
        "/audit/export", params={"format": "xml"}, headers={"X-API-Key": "admin-bootstrap-key"}
    )
    assert resp.status_code == 400


async def test_export_jsonl_admin(app_client):
    resp = await app_client.get("/audit/export", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")

    lines = [line for line in resp.text.strip().split("\n") if line]
    rows = [json.loads(line) for line in lines]
    tool_names = {r["tool_name"] for r in rows}
    assert "disable_user" in tool_names
    # the export call itself must show up too (recorded before streaming starts)
    assert "audit_log_exported" in tool_names


async def test_export_csv_admin(app_client):
    resp = await app_client.get(
        "/audit/export", params={"format": "csv"}, headers={"X-API-Key": "admin-bootstrap-key"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "tool_name" in resp.text
    assert "disable_user" in resp.text


async def test_export_records_its_own_security_event(app_client):
    await app_client.get(
        "/audit/export", params={"format": "csv"}, headers={"X-API-Key": "admin-bootstrap-key"}
    )
    async with db_session_module.session_scope() as session:
        rows = (
            await session.scalars(select(AuditLog).where(AuditLog.tool_name == "audit_log_exported"))
        ).all()
    assert len(rows) == 1
    assert "csv" in rows[0].result
