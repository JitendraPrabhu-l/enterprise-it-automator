"""Tests for two related fixes found from a live bug report: a demo-key
visitor could see the REAL company employee directory (GET /employees had
no caller-scoping at all — any authenticated caller, ADMIN or not, saw
every employee's full name/email/department/access grants), and there was
no way for a demo visitor to actually decide their OWN sensitive-action
approvals without either being handed a real reviewer token (defeats the
point of a low-privilege demo key) or leaving every demo ticket stuck.

Fixes: EmployeeUser.owned_by_client_id (set by identity_create_user when
the triggering ticket has a Ticket.submitted_by_client_id) scopes
GET /employees the same way Ticket.submitted_by_client_id already scopes
GET /tickets — ADMIN sees everyone, STANDARD/demo only sees what it itself
created. A seeded public demo Reviewer (role=IT_ADMIN by rbac.py's rule,
but further confined by app/api/main.py's _authorize_demo_reviewer_scope to
ONLY decide approvals on demo-owned tickets) closes the HITL gap without
ever letting a demo visitor touch a real approval.
"""

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import session as db_session_module
from app.db.models import (
    ApiClient,
    ApiClientRole,
    Approval,
    ApprovalStatus,
    EmployeeUser,
    Ticket,
    TicketStatus,
)
from app.mcp_server.tools import create_user


@pytest.fixture
async def app_client(monkeypatch, tmp_path):
    db_path = tmp_path / "employee_scoping_test.db"
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

    async with db_session_module.session_scope() as session:
        session.add(ApiClient(name="hr@example.com", role=ApiClientRole.STANDARD, key="hr-standard-key"))

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, main_module

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def _client_id_for_key(key: str) -> int:
    async with db_session_module.session_scope() as session:
        row = await session.scalar(select(ApiClient).where(ApiClient.key == key))
        return row.id


async def _seed_employee(username: str, owned_by_client_id: int | None) -> int:
    async with db_session_module.session_scope() as session:
        employee = EmployeeUser(
            username=username, full_name=username, email=f"{username}@example.com",
            department="Engineering", owned_by_client_id=owned_by_client_id,
        )
        session.add(employee)
        await session.flush()
        return employee.id


# --- GET /employees scoping ---------------------------------------------


async def test_admin_sees_every_employee_regardless_of_owner(app_client):
    ac, _ = app_client
    await _seed_employee("real1", owned_by_client_id=None)
    demo_id = await _client_id_for_key("public-demo-key")
    await _seed_employee("demo1", owned_by_client_id=demo_id)

    resp = await ac.get("/employees", headers={"X-API-Key": "admin-bootstrap-key"})
    assert resp.status_code == 200
    usernames = {e["username"] for e in resp.json()}
    assert usernames == {"real1", "demo1"}


async def test_standard_client_only_sees_employees_it_owns(app_client):
    """The original bug: a demo/STANDARD caller could see EVERY real
    employee. Confirm a STANDARD client now sees nothing it doesn't own."""
    ac, _ = app_client
    await _seed_employee("real1", owned_by_client_id=None)
    hr_id = await _client_id_for_key("hr-standard-key")
    await _seed_employee("hr-owned", owned_by_client_id=hr_id)

    resp = await ac.get("/employees", headers={"X-API-Key": "hr-standard-key"})
    assert resp.status_code == 200
    usernames = {e["username"] for e in resp.json()}
    assert usernames == {"hr-owned"}
    assert "real1" not in usernames


async def test_demo_key_cannot_see_real_company_employees(app_client):
    """Direct reproduction of the reported live bug: submit via the demo
    key must never reveal a real, unowned/other-owned employee record."""
    ac, _ = app_client
    await _seed_employee("jsmith", owned_by_client_id=None)
    await _seed_employee("rjones", owned_by_client_id=None)
    hr_id = await _client_id_for_key("hr-standard-key")
    await _seed_employee("hr-employee", owned_by_client_id=hr_id)

    resp = await ac.get("/employees", headers={"X-API-Key": "public-demo-key"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_demo_key_sees_only_employees_it_created(app_client):
    ac, _ = app_client
    demo_id = await _client_id_for_key("public-demo-key")
    await _seed_employee("real1", owned_by_client_id=None)
    await _seed_employee("demo-created", owned_by_client_id=demo_id)

    resp = await ac.get("/employees", headers={"X-API-Key": "public-demo-key"})
    assert resp.status_code == 200
    usernames = {e["username"] for e in resp.json()}
    assert usernames == {"demo-created"}


async def test_employees_status_filter_still_works_alongside_scoping(app_client):
    ac, _ = app_client
    hr_id = await _client_id_for_key("hr-standard-key")
    async with db_session_module.session_scope() as session:
        session.add(EmployeeUser(
            username="active1", full_name="a", email="a@example.com",
            owned_by_client_id=hr_id, status="active",
        ))
        session.add(EmployeeUser(
            username="disabled1", full_name="d", email="d@example.com",
            owned_by_client_id=hr_id, status="disabled",
        ))

    resp = await ac.get("/employees?status=active", headers={"X-API-Key": "hr-standard-key"})
    assert resp.status_code == 200
    usernames = {e["username"] for e in resp.json()}
    assert usernames == {"active1"}


# --- identity_create_user sets ownership from the ticket's client -------


async def test_create_user_sets_owned_by_client_id_from_ticket(app_client):
    demo_id = await _client_id_for_key("public-demo-key")
    async with db_session_module.session_scope() as session:
        ticket = Ticket(
            requester="anyone@example.com", subject="s", body="b",
            status=TicketStatus.EXECUTING, submitted_by_client_id=demo_id,
        )
        session.add(ticket)
        await session.flush()
        ticket_id = ticket.id

        await create_user(
            session, username="newhire", full_name="New Hire", email="newhire@example.com",
            department="Engineering", ticket_id=ticket_id,
        )

    async with db_session_module.session_scope() as session:
        employee = await session.scalar(select(EmployeeUser).where(EmployeeUser.username == "newhire"))
        assert employee.owned_by_client_id == demo_id


async def test_create_user_leaves_owned_by_client_id_null_when_ticket_has_no_client(app_client):
    async with db_session_module.session_scope() as session:
        ticket = Ticket(
            requester="anyone@example.com", subject="s", body="b",
            status=TicketStatus.EXECUTING, submitted_by_client_id=None,
        )
        session.add(ticket)
        await session.flush()
        ticket_id = ticket.id

        await create_user(
            session, username="unowned-hire", full_name="X", email="x@example.com",
            ticket_id=ticket_id,
        )

    async with db_session_module.session_scope() as session:
        employee = await session.scalar(select(EmployeeUser).where(EmployeeUser.username == "unowned-hire"))
        assert employee.owned_by_client_id is None


async def test_create_user_leaves_owned_by_client_id_null_with_no_ticket_id(app_client):
    async with db_session_module.session_scope() as session:
        await create_user(session, username="no-ticket-hire", full_name="X", email="x@example.com")

    async with db_session_module.session_scope() as session:
        employee = await session.scalar(select(EmployeeUser).where(EmployeeUser.username == "no-ticket-hire"))
        assert employee.owned_by_client_id is None


# --- demo reviewer: /demo-key issues a working reviewer_token ------------


async def test_demo_key_endpoint_issues_a_reviewer_token(app_client):
    ac, _ = app_client
    resp = await ac.get("/demo-key")
    data = resp.json()
    assert data["reviewer_token"]

    async with db_session_module.session_scope() as session:
        from app.db.models import Reviewer

        reviewer = await session.scalar(select(Reviewer).where(Reviewer.token == data["reviewer_token"]))
    assert reviewer is not None
    assert reviewer.username == "public-demo-reviewer"


async def test_ensure_demo_reviewer_is_idempotent(app_client):
    _, main_module = app_client
    await main_module._ensure_demo_reviewer()  # must not raise / duplicate

    from app.db.models import Reviewer

    async with db_session_module.session_scope() as session:
        rows = list(
            await session.scalars(select(Reviewer).where(Reviewer.username == "public-demo-reviewer"))
        )
    assert len(rows) == 1


# --- demo reviewer: confined to demo-owned approvals only ----------------


async def _seed_pending_approval(*, submitted_by_client_id: int | None) -> int:
    async with db_session_module.session_scope() as session:
        ticket = Ticket(
            requester="anyone@example.com", subject="s", body="b",
            status=TicketStatus.AWAITING_APPROVAL, submitted_by_client_id=submitted_by_client_id,
        )
        session.add(ticket)
        await session.flush()
        approval = Approval(
            ticket_id=ticket.id, tool_name="disable_user", tool_args={"username": "someone"},
            status=ApprovalStatus.PENDING,
        )
        session.add(approval)
        await session.flush()
        return approval.id


async def _demo_reviewer_token() -> str:
    from app.db.models import Reviewer

    async with db_session_module.session_scope() as session:
        row = await session.scalar(select(Reviewer).where(Reviewer.username == "public-demo-reviewer"))
        return row.token


async def test_demo_reviewer_can_decide_a_demo_owned_approval(app_client, monkeypatch):
    ac, main_module = app_client
    demo_id = await _client_id_for_key("public-demo-key")
    approval_id = await _seed_pending_approval(submitted_by_client_id=demo_id)
    token = await _demo_reviewer_token()

    async def _fake_resume(ticket_id):
        return {
            "ticket_id": ticket_id, "done": True, "plan": [], "results": [],
            "error": None, "interrupted": False, "pending_approval": None,
        }

    monkeypatch.setattr(main_module, "resume_ticket_run", _fake_resume)

    resp = await ac.post(
        f"/approvals/{approval_id}/decide",
        json={"approve": True},
        headers={"X-API-Key": "public-demo-key", "X-Reviewer-Token": token},
    )
    assert resp.status_code == 200


async def test_demo_reviewer_cannot_decide_a_real_approval(app_client):
    """The core safety property: the publicly-served demo reviewer token
    must never be able to approve/reject a REAL, non-demo sensitive
    action — even though its role is IT_ADMIN (which would normally be
    allowed to decide any approval per app/api/rbac.py)."""
    ac, _ = app_client
    hr_id = await _client_id_for_key("hr-standard-key")
    approval_id = await _seed_pending_approval(submitted_by_client_id=hr_id)
    token = await _demo_reviewer_token()

    resp = await ac.post(
        f"/approvals/{approval_id}/decide",
        json={"approve": True},
        headers={"X-API-Key": "public-demo-key", "X-Reviewer-Token": token},
    )
    assert resp.status_code == 403

    async with db_session_module.session_scope() as session:
        approval = await session.get(Approval, approval_id)
        assert approval.status == ApprovalStatus.PENDING, "must remain undecided after the rejection"


async def test_demo_reviewer_cannot_decide_an_unowned_approval(app_client):
    """A ticket with submitted_by_client_id=None (e.g. pre-existing, or
    submitted under an unset/local API_KEY) is NOT demo-owned — the demo
    reviewer must not be able to decide it either."""
    ac, _ = app_client
    approval_id = await _seed_pending_approval(submitted_by_client_id=None)
    token = await _demo_reviewer_token()

    resp = await ac.post(
        f"/approvals/{approval_id}/decide",
        json={"approve": True},
        headers={"X-API-Key": "public-demo-key", "X-Reviewer-Token": token},
    )
    assert resp.status_code == 403


async def test_real_it_admin_reviewer_still_unaffected_by_demo_scope_check(app_client):
    """_authorize_demo_reviewer_scope must be a no-op for every reviewer
    OTHER than the seeded demo one — a real it_admin must keep deciding
    any approval, demo-owned or not."""
    ac, _ = app_client
    from app.db.models import Reviewer, ReviewerRole

    async with db_session_module.session_scope() as session:
        session.add(Reviewer(username="real-admin", role=ReviewerRole.IT_ADMIN, token="real-admin-token"))

    demo_id = await _client_id_for_key("public-demo-key")
    approval_id = await _seed_pending_approval(submitted_by_client_id=demo_id)

    async def _fake_resume(ticket_id):
        return {
            "ticket_id": ticket_id, "done": True, "plan": [], "results": [],
            "error": None, "interrupted": False, "pending_approval": None,
        }

    _, main_module = app_client
    main_module.resume_ticket_run = _fake_resume

    resp = await ac.post(
        f"/approvals/{approval_id}/decide",
        json={"approve": True},
        headers={"X-API-Key": "admin-bootstrap-key", "X-Reviewer-Token": "real-admin-token"},
    )
    assert resp.status_code == 200
