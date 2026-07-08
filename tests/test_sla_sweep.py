"""Tests for the SLA-timeout/stuck-ticket sweep (Stage 4.5, scoped down —
see app/agent/sla_sweep.py's module docstring).

Uses an isolated on-disk SQLite DB per test (same pattern as
test_fanout.py's test_mixed_plan_batches_non_sensitive_then_gates_sensitive_step)
since sla_sweep.py goes through app.db.session's module-level
engine/session-factory singletons rather than the plain `session` fixture.
"""

import datetime as dt

import pytest

from app.agent.sla_sweep import (
    STUCK_TICKET_THRESHOLD_MINUTES,
    run_sla_sweep,
    sweep_overdue_approvals,
    sweep_stuck_tickets,
)
from app.db.models import Approval, ApprovalStatus, AuditLog, Ticket, TicketStatus


@pytest.fixture
async def isolated_db(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.db import session as db_session_module

    db_path = tmp_path / "sla_sweep_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    await db_session_module.init_db()
    yield db_session_module
    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def test_sweep_escalates_overdue_pending_approval(isolated_db):
    async with isolated_db.session_scope() as session:
        session.add(Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL))
        session.add(
            Approval(
                id=1, ticket_id=1, tool_name="disable_user", tool_args={"username": "jsmith"},
                status=ApprovalStatus.PENDING, sla_deadline=_now() - dt.timedelta(minutes=1),
            )
        )

    escalated = await sweep_overdue_approvals()
    assert escalated == [1]

    async with isolated_db.session_scope() as session:
        approval = await session.get(Approval, 1)
        assert approval.status == ApprovalStatus.ESCALATED
        assert approval.escalated_at is not None


async def test_sweep_leaves_pending_approval_within_sla_untouched(isolated_db):
    async with isolated_db.session_scope() as session:
        session.add(Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL))
        session.add(
            Approval(
                id=1, ticket_id=1, tool_name="disable_user", tool_args={"username": "jsmith"},
                status=ApprovalStatus.PENDING, sla_deadline=_now() + dt.timedelta(hours=1),
            )
        )

    escalated = await sweep_overdue_approvals()
    assert escalated == []

    async with isolated_db.session_scope() as session:
        approval = await session.get(Approval, 1)
        assert approval.status == ApprovalStatus.PENDING


async def test_sweep_ignores_already_resolved_approvals(isolated_db):
    async with isolated_db.session_scope() as session:
        session.add(Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.COMPLETED))
        session.add(
            Approval(
                id=1, ticket_id=1, tool_name="disable_user", tool_args={"username": "jsmith"},
                status=ApprovalStatus.APPROVED, sla_deadline=_now() - dt.timedelta(hours=2),
            )
        )

    escalated = await sweep_overdue_approvals()
    assert escalated == []


async def test_sweep_writes_audit_log_entry_for_escalation(isolated_db):
    async with isolated_db.session_scope() as session:
        session.add(Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL))
        session.add(
            Approval(
                id=1, ticket_id=1, tool_name="disable_user", tool_args={"username": "jsmith"},
                status=ApprovalStatus.PENDING, sla_deadline=_now() - dt.timedelta(minutes=1),
            )
        )

    await sweep_overdue_approvals()

    async with isolated_db.session_scope() as session:
        from sqlalchemy import select

        entries = (await session.scalars(select(AuditLog).where(AuditLog.ticket_id == 1))).all()
        assert len(entries) == 1
        assert entries[0].actor == "sla_sweep"
        assert "escalated" in entries[0].result


async def test_sweep_flags_stuck_ticket_past_threshold(isolated_db):
    stale = _now() - dt.timedelta(minutes=STUCK_TICKET_THRESHOLD_MINUTES + 5)
    async with isolated_db.session_scope() as session:
        ticket = Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.PLANNING)
        session.add(ticket)
        await session.flush()
        # updated_at has an onupdate default that fires on flush/commit —
        # overwrite it directly afterward so the row is actually stale.
        await session.execute(
            Ticket.__table__.update().where(Ticket.__table__.c.id == 1).values(updated_at=stale)
        )

    stuck = await sweep_stuck_tickets()
    assert stuck == [1]


async def test_sweep_marks_stuck_ticket_failed_not_just_audited(isolated_db):
    """Found live: a ticket orphaned by a process restart mid-run stayed
    frozen in "planning" in the dashboard forever — this sweep used to only
    write an audit entry and never touch ticket.status, so nothing ever
    gave the caller an actionable terminal state. Confirms the ticket
    itself now transitions to FAILED with an explanatory result_summary,
    not just a log entry nobody browsing the dashboard would see."""
    stale = _now() - dt.timedelta(minutes=STUCK_TICKET_THRESHOLD_MINUTES + 5)
    async with isolated_db.session_scope() as session:
        ticket = Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.PLANNING)
        session.add(ticket)
        await session.flush()
        await session.execute(
            Ticket.__table__.update().where(Ticket.__table__.c.id == 1).values(updated_at=stale)
        )

    await sweep_stuck_tickets()

    async with isolated_db.session_scope() as session:
        ticket = await session.get(Ticket, 1)
        assert ticket.status == TicketStatus.FAILED
        assert "stuck" in ticket.result_summary.lower()
        assert "planning" in ticket.result_summary.lower()


async def test_sweep_marks_stuck_executing_ticket_failed_too(isolated_db):
    stale = _now() - dt.timedelta(minutes=STUCK_TICKET_THRESHOLD_MINUTES + 5)
    async with isolated_db.session_scope() as session:
        ticket = Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.EXECUTING)
        session.add(ticket)
        await session.flush()
        await session.execute(
            Ticket.__table__.update().where(Ticket.__table__.c.id == 1).values(updated_at=stale)
        )

    stuck = await sweep_stuck_tickets()
    assert stuck == [1]

    async with isolated_db.session_scope() as session:
        ticket = await session.get(Ticket, 1)
        assert ticket.status == TicketStatus.FAILED


async def test_sweep_ignores_recently_updated_planning_ticket(isolated_db):
    async with isolated_db.session_scope() as session:
        session.add(Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.PLANNING))

    stuck = await sweep_stuck_tickets()
    assert stuck == []


async def test_sweep_ignores_stale_completed_ticket(isolated_db):
    stale = _now() - dt.timedelta(minutes=STUCK_TICKET_THRESHOLD_MINUTES + 5)
    async with isolated_db.session_scope() as session:
        session.add(Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.COMPLETED))
        await session.flush()
        await session.execute(
            Ticket.__table__.update().where(Ticket.__table__.c.id == 1).values(updated_at=stale)
        )

    stuck = await sweep_stuck_tickets()
    assert stuck == []


async def test_run_sla_sweep_combines_both_checks(isolated_db):
    stale = _now() - dt.timedelta(minutes=STUCK_TICKET_THRESHOLD_MINUTES + 5)
    async with isolated_db.session_scope() as session:
        session.add(Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL))
        session.add(
            Approval(
                id=1, ticket_id=1, tool_name="disable_user", tool_args={"username": "jsmith"},
                status=ApprovalStatus.PENDING, sla_deadline=_now() - dt.timedelta(minutes=1),
            )
        )
        session.add(Ticket(id=2, requester="hr@x.com", subject="s2", body="b2", status=TicketStatus.PLANNING))
        await session.flush()
        await session.execute(
            Ticket.__table__.update().where(Ticket.__table__.c.id == 2).values(updated_at=stale)
        )

    result = await run_sla_sweep()
    assert result == {"escalated_approvals": [1], "stuck_tickets": [2]}
