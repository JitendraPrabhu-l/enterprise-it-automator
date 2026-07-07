"""Tests for ApiClient-based per-caller scoping and the daily request cap,
added after a security review found:

- GET /tickets/{id}, GET /tickets/{id}/audit, and GET /approvals had no
  caller-scoping at all — any holder of the one shared API key could read
  every employee's ticket/audit/access history, not just their own.
- There was no bound on sustained request volume per caller beyond the
  per-minute rate limit, allowing indefinite LLM-cost amplification from a
  single caller submitting maximum-length tickets in a loop.

Uses the same real-ASGI-client pattern as test_ag_ui_endpoints.py rather
than calling route functions directly, since the actual behavior under
test is end-to-end: does a STANDARD ApiClient's key really get a 404 for
someone else's ticket over real HTTP, not just in isolated unit logic.

Tickets are seeded directly into the DB rather than through POST /tickets
for the read-scoping tests — that route drives the real LangGraph agent
(classification/planning LLM calls), which is unrelated to what these
tests verify and would need a real or heavily-mocked LLM. The daily-
request-cap tests DO need to go through POST /tickets (that's what the cap
gates), so those monkeypatch app.agent.runner.start_ticket_run to a fast
stub instead of running the real graph.
"""

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import session as db_session_module


@pytest.fixture
async def client(monkeypatch, tmp_path):
    db_path = tmp_path / "api_client_scoping_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("API_KEY", "admin-bootstrap-key")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    import app.api.main as main_module

    await db_session_module.init_db()
    await main_module._ensure_bootstrap_admin_client()

    from app.db.models import ApiClient, ApiClientRole

    async with db_session_module.session_scope() as session:
        session.add(ApiClient(name="hr@example.com", role=ApiClientRole.STANDARD, key="hr-standard-key"))
        session.add(ApiClient(name="finance@example.com", role=ApiClientRole.STANDARD, key="finance-standard-key"))

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, main_module

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def _seed_ticket(requester: str) -> int:
    from app.db.models import Ticket, TicketStatus

    async with db_session_module.session_scope() as session:
        ticket = Ticket(requester=requester, subject="s", body="b", status=TicketStatus.COMPLETED)
        session.add(ticket)
        await session.flush()
        return ticket.id


async def test_admin_client_sees_ticket_filed_by_anyone(client):
    ac, _ = client
    ticket_id = await _seed_ticket("hr@example.com")

    resp = await ac.get(f"/tickets/{ticket_id}", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    assert resp.json()["requester"] == "hr@example.com"


async def test_standard_client_sees_its_own_ticket(client):
    ac, _ = client
    ticket_id = await _seed_ticket("hr@example.com")

    resp = await ac.get(f"/tickets/{ticket_id}", headers={"X-API-Key": "hr-standard-key"})
    assert resp.status_code == 200
    assert resp.json()["requester"] == "hr@example.com"


async def test_standard_client_cannot_see_another_clients_ticket(client):
    """The core IDOR fix: hr@example.com's key must not be able to read a
    ticket filed by finance@example.com — a 404, not a 403, so as not to
    even confirm the ticket ID exists to a caller who shouldn't see it."""
    ac, _ = client
    ticket_id = await _seed_ticket("hr@example.com")

    resp = await ac.get(f"/tickets/{ticket_id}", headers={"X-API-Key": "finance-standard-key"})
    assert resp.status_code == 404


async def test_standard_client_list_tickets_only_shows_own(client):
    ac, _ = client
    await _seed_ticket("hr@example.com")
    await _seed_ticket("finance@example.com")

    resp = await ac.get("/tickets", headers={"X-API-Key": "hr-standard-key"})
    assert resp.status_code == 200
    requesters = {t["requester"] for t in resp.json()}
    assert requesters == {"hr@example.com"}


async def test_admin_client_list_tickets_shows_everyone(client):
    ac, _ = client
    await _seed_ticket("hr@example.com")
    await _seed_ticket("finance@example.com")

    resp = await ac.get("/tickets", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    requesters = {t["requester"] for t in resp.json()}
    assert requesters == {"hr@example.com", "finance@example.com"}


async def test_standard_client_cannot_read_another_clients_ticket_audit(client):
    ac, _ = client
    ticket_id = await _seed_ticket("hr@example.com")

    resp = await ac.get(f"/tickets/{ticket_id}/audit", headers={"X-API-Key": "finance-standard-key"})
    assert resp.status_code == 404


async def test_standard_client_can_read_its_own_ticket_audit(client):
    ac, _ = client
    ticket_id = await _seed_ticket("hr@example.com")

    resp = await ac.get(f"/tickets/{ticket_id}/audit", headers={"X-API-Key": "hr-standard-key"})
    assert resp.status_code == 200


async def test_standard_client_approvals_list_scoped_to_own_tickets(client):
    ac, _ = client
    from app.db.models import Approval, Ticket, TicketStatus

    async with db_session_module.session_scope() as session:
        hr_ticket = Ticket(requester="hr@example.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL)
        finance_ticket = Ticket(
            requester="finance@example.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL
        )
        session.add_all([hr_ticket, finance_ticket])
        await session.flush()
        session.add_all(
            [
                Approval(ticket_id=hr_ticket.id, tool_name="disable_user", tool_args={"username": "a"}),
                Approval(ticket_id=finance_ticket.id, tool_name="disable_user", tool_args={"username": "b"}),
            ]
        )

    resp = await ac.get("/approvals", headers={"X-API-Key": "hr-standard-key"})
    assert resp.status_code == 200
    approvals = resp.json()
    assert len(approvals) == 1
    assert approvals[0]["tool_args"]["username"] == "a"


async def test_admin_client_approvals_list_sees_everything(client):
    ac, _ = client
    from app.db.models import Approval, Ticket, TicketStatus

    async with db_session_module.session_scope() as session:
        hr_ticket = Ticket(requester="hr@example.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL)
        finance_ticket = Ticket(
            requester="finance@example.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL
        )
        session.add_all([hr_ticket, finance_ticket])
        await session.flush()
        session.add_all(
            [
                Approval(ticket_id=hr_ticket.id, tool_name="disable_user", tool_args={"username": "a"}),
                Approval(ticket_id=finance_ticket.id, tool_name="disable_user", tool_args={"username": "b"}),
            ]
        )

    resp = await ac.get("/approvals", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def _patch_start_ticket_run(monkeypatch, main_module):
    """Ticket submission (POST /tickets) drives the real LangGraph agent —
    irrelevant to what the daily-request-cap tests verify (whether the cap
    itself is enforced), so replace it with a fast stub rather than pay for
    real classification/planning LLM calls."""

    async def _fake_start_ticket_run(ticket_id, ticket_text):
        return {
            "ticket_id": ticket_id, "done": True, "plan": [], "results": [],
            "error": None, "interrupted": False, "pending_approval": None,
        }

    monkeypatch.setattr(main_module, "start_ticket_run", _fake_start_ticket_run)


async def test_daily_request_limit_enforced_for_standard_client(client, monkeypatch):
    ac, main_module = client
    _patch_start_ticket_run(monkeypatch, main_module)
    from app.db.models import ApiClient

    async with db_session_module.session_scope() as session:
        row = await session.scalar(select(ApiClient).where(ApiClient.name == "hr@example.com"))
        row.daily_request_limit = 2

    resp1 = await ac.post(
        "/tickets", json={"requester": "hr@example.com", "subject": "s1", "body": "b1"},
        headers={"X-API-Key": "hr-standard-key"},
    )
    assert resp1.status_code == 200

    resp2 = await ac.post(
        "/tickets", json={"requester": "hr@example.com", "subject": "s2", "body": "b2"},
        headers={"X-API-Key": "hr-standard-key"},
    )
    assert resp2.status_code == 200

    resp3 = await ac.post(
        "/tickets", json={"requester": "hr@example.com", "subject": "s3", "body": "b3"},
        headers={"X-API-Key": "hr-standard-key"},
    )
    assert resp3.status_code == 429


async def test_daily_request_limit_is_per_client_not_global(client, monkeypatch):
    """finance's requests must not count against hr's budget — otherwise
    one caller could exhaust another's daily allowance just by being
    active, which would be its own denial-of-service vector."""
    ac, main_module = client
    _patch_start_ticket_run(monkeypatch, main_module)
    from app.db.models import ApiClient

    async with db_session_module.session_scope() as session:
        row = await session.scalar(select(ApiClient).where(ApiClient.name == "hr@example.com"))
        row.daily_request_limit = 1

    resp1 = await ac.post(
        "/tickets", json={"requester": "hr@example.com", "subject": "s1", "body": "b1"},
        headers={"X-API-Key": "hr-standard-key"},
    )
    assert resp1.status_code == 200

    resp2 = await ac.post(
        "/tickets", json={"requester": "finance@example.com", "subject": "s2", "body": "b2"},
        headers={"X-API-Key": "finance-standard-key"},
    )
    assert resp2.status_code == 200, "a different client's own budget must be unaffected"


async def test_admin_client_not_subject_to_daily_request_limit_check_bypass(client, monkeypatch):
    """Not exempting admin from the mechanism entirely — just confirms the
    default limit (100/day) doesn't trip during ordinary test usage."""
    ac, main_module = client
    _patch_start_ticket_run(monkeypatch, main_module)
    resp = await ac.post(
        "/tickets", json={"requester": "someone@example.com", "subject": "s", "body": "b"},
        headers={"X-API-Key": "admin-bootstrap-key"},
    )
    assert resp.status_code == 200
