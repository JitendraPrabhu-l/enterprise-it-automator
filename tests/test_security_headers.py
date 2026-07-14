"""Tests for app/api/main.py's security_headers_middleware — standard
defense-in-depth response headers sent on every request, regardless of
route or auth outcome.
"""

import httpx
import pytest

from app.config import get_settings
from app.db import session as db_session_module


@pytest.fixture
async def client(monkeypatch, tmp_path):
    db_path = tmp_path / "security_headers_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.delenv("API_KEY", raising=False)
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    import app.api.main as main_module

    await db_session_module.init_db()

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_security_headers_present_on_success(client):
    resp = await client.get("/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "max-age=" in resp.headers["Strict-Transport-Security"]
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


async def test_security_headers_present_on_404(client):
    resp = await client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


async def test_security_headers_present_on_auth_failure(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "real-key")
    get_settings.cache_clear()
    resp = await client.get("/tickets/1")
    assert resp.status_code == 401
    assert resp.headers["X-Frame-Options"] == "DENY"
    get_settings.cache_clear()
