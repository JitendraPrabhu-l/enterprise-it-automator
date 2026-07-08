"""Tests for daily reset of the public demo client's own data
(app/agent/demo_purge.py) — the third piece of keeping demo traffic from
mixing with real operational data, alongside the read-scoping
(Ticket.submitted_by_client_id) and daily request cap already in place.
Demo tickets/approvals/audit entries are hard-deleted once a day rather
than accumulating forever; ADMIN's default dashboard view also hides the
demo client's own data (opt back in via ?include_demo=true) between resets.

Deliberately narrow: this must NEVER touch a real ApiClient's data — there
is no general "clean up old tickets" feature here, only a reset scoped to
the one client DEMO_API_KEY identifies.
"""

import datetime as dt

import httpx
import pytest
from sqlalchemy import select

from app.agent.demo_purge import reset_demo_data_if_due
from app.config import get_settings
from app.db import session as db_session_module
from app.db.models import ApiClient, ApiClientRole, Approval, AuditLog, Ticket, TicketStatus


@pytest.fixture
async def app_client(monkeypatch, tmp_path):
    db_path = tmp_path / "demo_purge_test.db"
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

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, main_module

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def _demo_client_id() -> int:
    async with db_session_module.session_scope() as session:
        row = await session.scalar(select(ApiClient).where(ApiClient.key == "public-demo-key"))
        return row.id


async def _seed_demo_ticket_with_approval_and_audit() -> int:
    demo_id = await _demo_client_id()
    async with db_session_module.session_scope() as session:
        ticket = Ticket(
            requester="anyone@example.com", subject="s", body="b", status=TicketStatus.COMPLETED,
            submitted_by_client_id=demo_id,
        )
        session.add(ticket)
        await session.flush()
        session.add(Approval(ticket_id=ticket.id, tool_name="disable_user", tool_args={"username": "x"}))
        session.add(AuditLog(ticket_id=ticket.id, actor="mcp-client", tool_name="disable_user", success=True))
        return ticket.id


async def _seed_real_ticket() -> int:
    async with db_session_module.session_scope() as session:
        session.add(ApiClient(name="hr@example.com", role=ApiClientRole.STANDARD, key="hr-key"))
        await session.flush()
        hr_row = await session.scalar(select(ApiClient).where(ApiClient.key == "hr-key"))
        ticket = Ticket(
            requester="hr@example.com", subject="s", body="b", status=TicketStatus.COMPLETED,
            submitted_by_client_id=hr_row.id,
        )
        session.add(ticket)
        await session.flush()
        return ticket.id


async def test_reset_is_noop_when_demo_api_key_unset(monkeypatch, app_client):
    monkeypatch.delenv("DEMO_API_KEY", raising=False)
    get_settings.cache_clear()
    purged = await reset_demo_data_if_due()
    assert purged == 0


async def test_reset_purges_a_brand_new_demo_clients_data_immediately(app_client):
    """data_last_purged_at is NULL for a client that's never been purged —
    the very first check must reset it (not wait a full interval from some
    non-existent prior purge time)."""
    ticket_id = await _seed_demo_ticket_with_approval_and_audit()

    purged = await reset_demo_data_if_due()
    assert purged == 1

    async with db_session_module.session_scope() as session:
        assert await session.get(Ticket, ticket_id) is None


async def test_reset_deletes_approvals_and_audit_entries_too(app_client):
    """Ticket's ORM cascade="all, delete-orphan" only fires on an ORM-level
    delete — the purge uses a bulk DELETE, so approvals/audit rows must be
    deleted explicitly or they'd violate the FK constraint / be orphaned."""
    ticket_id = await _seed_demo_ticket_with_approval_and_audit()

    await reset_demo_data_if_due()

    async with db_session_module.session_scope() as session:
        approvals = list(await session.scalars(select(Approval).where(Approval.ticket_id == ticket_id)))
        audit_entries = list(await session.scalars(select(AuditLog).where(AuditLog.ticket_id == ticket_id)))
    assert approvals == []
    assert audit_entries == []


async def test_reset_never_touches_a_real_clients_ticket(app_client):
    """The core safety property: a real ApiClient's ticket must survive a
    demo reset untouched, even when both exist side by side."""
    demo_ticket_id = await _seed_demo_ticket_with_approval_and_audit()
    real_ticket_id = await _seed_real_ticket()

    purged = await reset_demo_data_if_due()
    assert purged == 1

    async with db_session_module.session_scope() as session:
        assert await session.get(Ticket, demo_ticket_id) is None
        real_ticket = await session.get(Ticket, real_ticket_id)
        assert real_ticket is not None
        assert real_ticket.requester == "hr@example.com"


async def test_reset_not_due_again_immediately_after_running(app_client):
    await _seed_demo_ticket_with_approval_and_audit()
    first = await reset_demo_data_if_due()
    assert first == 1

    await _seed_demo_ticket_with_approval_and_audit()
    second = await reset_demo_data_if_due()
    assert second == 0, "must not purge again before the reset interval has elapsed"

    async with db_session_module.session_scope() as session:
        remaining = list(await session.scalars(select(Ticket)))
    assert len(remaining) == 1, "the second seeded ticket must still be present, unpurged"


async def test_reset_fires_again_once_the_interval_has_elapsed(monkeypatch, app_client):
    monkeypatch.setenv("DEMO_DATA_RESET_HOURS", "1")
    get_settings.cache_clear()

    await _seed_demo_ticket_with_approval_and_audit()
    await reset_demo_data_if_due()

    demo_id = await _demo_client_id()
    async with db_session_module.session_scope() as session:
        row = await session.get(ApiClient, demo_id)
        row.data_last_purged_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)

    await _seed_demo_ticket_with_approval_and_audit()
    purged_again = await reset_demo_data_if_due()
    assert purged_again == 1


async def test_reset_also_resets_the_daily_request_count(app_client):
    """A side effect worth pinning: the demo client's request-count budget
    resets alongside its data, not on a separate independent clock — one
    daily reset event for the demo client, not two uncoordinated ones."""
    demo_id = await _demo_client_id()
    async with db_session_module.session_scope() as session:
        row = await session.get(ApiClient, demo_id)
        row.daily_request_count = 9

    await reset_demo_data_if_due()

    async with db_session_module.session_scope() as session:
        row = await session.get(ApiClient, demo_id)
        assert row.daily_request_count == 0


async def test_admin_default_view_hides_demo_tickets(app_client):
    ac, _ = app_client
    await _seed_demo_ticket_with_approval_and_audit()
    await _seed_real_ticket()

    resp = await ac.get("/tickets", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    requesters = {t["requester"] for t in resp.json()}
    assert requesters == {"hr@example.com"}


async def test_admin_can_opt_into_seeing_demo_tickets(app_client):
    ac, _ = app_client
    await _seed_demo_ticket_with_approval_and_audit()
    await _seed_real_ticket()

    resp = await ac.get("/tickets?include_demo=true", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    requesters = {t["requester"] for t in resp.json()}
    assert requesters == {"anyone@example.com", "hr@example.com"}


async def test_admin_default_view_still_shows_tickets_with_no_attributed_client(app_client):
    """A ticket with submitted_by_client_id=NULL (e.g. one predating that
    column, or API_KEY-unset local demo submissions) must NOT be
    accidentally hidden by the demo-exclusion filter — plain != would
    exclude NULL rows under SQL's three-valued logic; the filter must use
    IS DISTINCT FROM instead."""
    ac, _ = app_client
    async with db_session_module.session_scope() as session:
        orphan_ticket = Ticket(
            requester="orphan@example.com", subject="s", body="b", status=TicketStatus.COMPLETED,
            submitted_by_client_id=None,
        )
        session.add(orphan_ticket)

    resp = await ac.get("/tickets", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    requesters = {t["requester"] for t in resp.json()}
    assert "orphan@example.com" in requesters


async def test_admin_default_approvals_view_hides_demo_approvals(app_client):
    ac, _ = app_client
    await _seed_demo_ticket_with_approval_and_audit()

    resp = await ac.get("/approvals", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_admin_can_opt_into_seeing_demo_approvals(app_client):
    ac, _ = app_client
    await _seed_demo_ticket_with_approval_and_audit()

    resp = await ac.get("/approvals?include_demo=true", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_standard_client_include_demo_param_has_no_effect(app_client):
    """include_demo is an ADMIN-view concept — a STANDARD client's own
    scoping (only its own tickets) must be unaffected by the param either
    way, since it was never seeing anyone else's data regardless."""
    ac, _ = app_client
    await _seed_real_ticket()

    resp = await ac.get("/tickets?include_demo=true", headers={"X-API-Key": "hr-key"})
    assert resp.status_code == 200
    requesters = {t["requester"] for t in resp.json()}
    assert requesters == {"hr@example.com"}


async def test_trigger_demo_reset_endpoint_requires_admin(app_client):
    ac, _ = app_client
    await _seed_real_ticket()  # gives "hr@example.com" a real ApiClient row/key

    resp = await ac.post("/admin/demo-reset", headers={"X-API-Key": "hr-key"})
    assert resp.status_code == 403


async def test_trigger_demo_reset_endpoint_works_for_admin(app_client):
    ac, _ = app_client
    await _seed_demo_ticket_with_approval_and_audit()

    resp = await ac.post("/admin/demo-reset", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    assert resp.json() == {"tickets_purged": 1}
