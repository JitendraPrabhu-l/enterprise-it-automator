"""Tests for the public demo-key feature: GET /demo-key hands out a
low-privilege, low-daily-cap API key so a stranger visiting a public
deployment (e.g. a portfolio link a recruiter clicks) can try the app
without a real credential. Added after a real deployment was found
completely unauthenticated by accident (API_KEY unset in Render), which
raised the opposite concern: how does a genuine stranger try the app once
auth is correctly enforced?

DEMO_API_KEY is opt-in and blank by default — GET /demo-key must return
null unless a real deployment operator deliberately configures it.
"""

import httpx
import pytest

from app.config import get_settings
from app.db import session as db_session_module


@pytest.fixture
async def client(monkeypatch, tmp_path):
    db_path = tmp_path / "demo_api_key_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("API_KEY", "admin-bootstrap-key")
    monkeypatch.setenv("DEMO_API_KEY", "public-demo-key")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    import app.api.main as main_module

    await db_session_module.init_db()
    await main_module._ensure_bootstrap_admin_client()
    await main_module._ensure_demo_guest_client()
    await main_module._ensure_demo_reviewer()

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, main_module

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


@pytest.fixture
async def client_no_demo_key(monkeypatch, tmp_path):
    """Same setup, but DEMO_API_KEY left unset — the default, opt-in-only
    state for any deployment that hasn't deliberately configured this."""
    db_path = tmp_path / "no_demo_api_key_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("API_KEY", "admin-bootstrap-key")
    monkeypatch.delenv("DEMO_API_KEY", raising=False)
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    import app.api.main as main_module

    await db_session_module.init_db()
    await main_module._ensure_bootstrap_admin_client()
    await main_module._ensure_demo_guest_client()
    await main_module._ensure_demo_reviewer()

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, main_module

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_demo_key_endpoint_is_unauthenticated(client):
    """No X-API-Key header at all — this endpoint exists specifically so a
    caller with no key yet can get one."""
    ac, _ = client
    resp = await ac.get("/demo-key")
    assert resp.status_code == 200


async def test_demo_key_endpoint_returns_the_configured_key(client):
    ac, _ = client
    resp = await ac.get("/demo-key")
    data = resp.json()
    assert data["api_key"] == "public-demo-key"
    assert data["reviewer_token"], "a demo reviewer token must be issued alongside the demo API key"


async def test_demo_key_endpoint_returns_null_when_unconfigured(client_no_demo_key):
    ac, _ = client_no_demo_key
    resp = await ac.get("/demo-key")
    assert resp.json() == {"api_key": None, "reviewer_token": None}


async def test_demo_key_actually_authenticates_and_can_submit_tickets(client, monkeypatch):
    ac, main_module = client

    async def _fake_start_ticket_run(ticket_id, ticket_text):
        return {
            "ticket_id": ticket_id, "done": True, "plan": [], "results": [],
            "error": None, "interrupted": False, "pending_approval": None,
        }

    monkeypatch.setattr(main_module, "start_ticket_run", _fake_start_ticket_run)

    resp = await ac.post(
        "/tickets",
        json={"requester": "guest@example.com", "subject": "s", "body": "b"},
        headers={"X-API-Key": "public-demo-key"},
    )
    assert resp.status_code == 200


async def test_demo_key_client_only_sees_its_own_tickets(client):
    """The demo key must be genuinely low-privilege — scoped like any other
    STANDARD client, not secretly admin. Seed a ticket from someone else and
    confirm the demo key can't read it."""
    ac, _ = client
    from app.db.models import Ticket, TicketStatus

    async with db_session_module.session_scope() as session:
        other_ticket = Ticket(
            requester="someone-else@example.com", subject="s", body="b", status=TicketStatus.COMPLETED
        )
        session.add(other_ticket)
        await session.flush()
        other_ticket_id = other_ticket.id

    resp = await ac.get(f"/tickets/{other_ticket_id}", headers={"X-API-Key": "public-demo-key"})
    assert resp.status_code == 404


async def test_demo_key_has_a_low_daily_request_limit(client):
    from sqlalchemy import select

    from app.db.models import ApiClient
    from app.api.main import DEMO_CLIENT_DAILY_REQUEST_LIMIT

    async with db_session_module.session_scope() as session:
        demo_client = await session.scalar(select(ApiClient).where(ApiClient.key == "public-demo-key"))

    assert demo_client is not None
    assert demo_client.daily_request_limit == DEMO_CLIENT_DAILY_REQUEST_LIMIT
    assert demo_client.daily_request_limit < 100, "must be stricter than the normal default (100/day)"


async def test_ensure_demo_guest_client_is_idempotent(client):
    """Calling the bootstrap twice (e.g. across restarts) must not create a
    duplicate ApiClient row or crash on the unique key constraint."""
    ac, main_module = client
    await main_module._ensure_demo_guest_client()  # must not raise

    from sqlalchemy import select

    from app.db.models import ApiClient

    async with db_session_module.session_scope() as session:
        rows = list(await session.scalars(select(ApiClient).where(ApiClient.key == "public-demo-key")))
    assert len(rows) == 1
