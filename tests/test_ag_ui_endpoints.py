"""Tests for the two AG-UI-protocol SSE endpoints (POST /tickets/stream and
POST /approvals/{id}/decide/stream) added to app/api/main.py.

These exercise the FastAPI routing/auth/DB wiring around the streaming
endpoints — ticket creation, approval authorization, SSE framing/encoding —
via an in-process ASGI client with app.agent.ag_ui_bridge's
stream_ticket_run/stream_resume_run monkeypatched to fake AG-UI event
generators. The bridge's OWN correctness (translating a real graph run into
the right event sequence) is covered separately in test_ag_ui_bridge.py
against a real compiled graph — duplicating that here would just re-run the
same graph logic through an extra HTTP layer for no additional confidence.
"""

import json

import httpx
import pytest

from app.config import get_settings
from app.db import session as db_session_module


@pytest.fixture
async def client(monkeypatch, tmp_path):
    db_path = tmp_path / "ag_ui_endpoints_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("API_KEY", "test-api-key")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    import app.api.main as main_module

    await db_session_module.init_db()

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": "test-api-key"}
    ) as ac:
        yield ac, main_module

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


def _parse_sse(text: str) -> list[dict]:
    events = []
    for frame in text.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


async def test_tickets_stream_rejects_missing_api_key(client):
    ac, _ = client
    resp = await ac.post(
        "/tickets/stream",
        json={"requester": "hr@example.com", "subject": "s", "body": "b"},
        headers={"X-API-Key": ""},
    )
    assert resp.status_code == 401


async def test_tickets_stream_creates_ticket_and_streams_fake_events(client, monkeypatch):
    ac, main_module = client

    async def _fake_stream(ticket_id, ticket_text, run_id):
        from ag_ui.core import RunFinishedEvent, RunFinishedSuccessOutcome, RunStartedEvent

        yield RunStartedEvent(thread_id=f"ticket-{ticket_id}", run_id=run_id)
        yield RunFinishedEvent(
            thread_id=f"ticket-{ticket_id}", run_id=run_id, outcome=RunFinishedSuccessOutcome(),
            result={"done": True},
        )

    monkeypatch.setattr(main_module, "stream_ticket_run", _fake_stream)

    resp = await ac.post(
        "/tickets/stream", json={"requester": "hr@example.com", "subject": "Grant vpn", "body": "grant x vpn"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"

    tickets = await ac.get("/tickets")
    assert any(t["subject"] == "Grant vpn" for t in tickets.json())


async def test_decide_stream_rejects_missing_reviewer_token(client):
    ac, main_module = client
    from app.db.models import Approval, Ticket, TicketStatus

    async with db_session_module.session_scope() as session:
        ticket = Ticket(requester="hr@example.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL)
        session.add(ticket)
        await session.flush()
        approval = Approval(ticket_id=ticket.id, tool_name="disable_user", tool_args={"username": "x"})
        session.add(approval)
        await session.flush()
        approval_id = approval.id

    resp = await ac.post(f"/approvals/{approval_id}/decide/stream", json={"approve": True})
    assert resp.status_code == 401


async def test_decide_stream_rejection_path_streams_run_finished_without_touching_bridge(client, monkeypatch):
    """Rejecting an approval must not invoke stream_resume_run at all — the
    graph is never resumed for a rejected action (mirrors decide_approval's
    non-streaming behavior)."""
    ac, main_module = client
    from app.db.models import Approval, Reviewer, ReviewerRole, Ticket, TicketStatus

    async with db_session_module.session_scope() as session:
        reviewer = Reviewer(username="admin1", role=ReviewerRole.IT_ADMIN, token="reviewer-token-1")
        session.add(reviewer)
        ticket = Ticket(requester="hr@example.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL)
        session.add(ticket)
        await session.flush()
        approval = Approval(ticket_id=ticket.id, tool_name="disable_user", tool_args={"username": "x"})
        session.add(approval)
        await session.flush()
        approval_id = approval.id

    called = {"resume": False}

    async def _fake_resume(ticket_id, run_id):
        called["resume"] = True
        return
        yield  # pragma: no cover - never reached

    monkeypatch.setattr(main_module, "stream_resume_run", _fake_resume)

    resp = await ac.post(
        f"/approvals/{approval_id}/decide/stream",
        json={"approve": False},
        headers={"X-Reviewer-Token": "reviewer-token-1"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["result"]["error"] == "Rejected by reviewer"
    assert called["resume"] is False

    async with db_session_module.session_scope() as session:
        refreshed = await session.get(Approval, approval_id)
        assert refreshed.status.value == "rejected"


async def test_decide_stream_approval_path_invokes_resume_and_streams_its_events(client, monkeypatch):
    ac, main_module = client
    from app.db.models import Approval, Reviewer, ReviewerRole, Ticket, TicketStatus

    async with db_session_module.session_scope() as session:
        reviewer = Reviewer(username="admin2", role=ReviewerRole.IT_ADMIN, token="reviewer-token-2")
        session.add(reviewer)
        ticket = Ticket(requester="hr@example.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL)
        session.add(ticket)
        await session.flush()
        approval = Approval(ticket_id=ticket.id, tool_name="disable_user", tool_args={"username": "x"})
        session.add(approval)
        await session.flush()
        approval_id = approval.id
        ticket_id = ticket.id

    seen_args = {}

    async def _fake_resume(t_id, run_id):
        seen_args["ticket_id"] = t_id
        from ag_ui.core import RunFinishedEvent, RunFinishedSuccessOutcome, RunStartedEvent

        yield RunStartedEvent(thread_id=f"ticket-{t_id}", run_id=run_id)
        yield RunFinishedEvent(
            thread_id=f"ticket-{t_id}", run_id=run_id, outcome=RunFinishedSuccessOutcome(), result={"done": True}
        )

    monkeypatch.setattr(main_module, "stream_resume_run", _fake_resume)

    resp = await ac.post(
        f"/approvals/{approval_id}/decide/stream",
        json={"approve": True},
        headers={"X-Reviewer-Token": "reviewer-token-2"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert seen_args["ticket_id"] == ticket_id

    async with db_session_module.session_scope() as session:
        refreshed = await session.get(Approval, approval_id)
        assert refreshed.status.value == "approved"


async def test_decide_stream_rejects_already_decided_approval(client, monkeypatch):
    ac, main_module = client
    from app.db.models import Approval, ApprovalStatus, Reviewer, ReviewerRole, Ticket, TicketStatus

    async with db_session_module.session_scope() as session:
        reviewer = Reviewer(username="admin3", role=ReviewerRole.IT_ADMIN, token="reviewer-token-3")
        session.add(reviewer)
        ticket = Ticket(requester="hr@example.com", subject="s", body="b", status=TicketStatus.COMPLETED)
        session.add(ticket)
        await session.flush()
        approval = Approval(
            ticket_id=ticket.id, tool_name="disable_user", tool_args={"username": "x"},
            status=ApprovalStatus.APPROVED,
        )
        session.add(approval)
        await session.flush()
        approval_id = approval.id

    resp = await ac.post(
        f"/approvals/{approval_id}/decide/stream",
        json={"approve": True},
        headers={"X-Reviewer-Token": "reviewer-token-3"},
    )
    assert resp.status_code == 409
