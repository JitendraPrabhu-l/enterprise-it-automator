"""Regression test for a security-review finding: authorize_reviewer
(app/api/rbac.py) trusts approval.tool_args["username"] as ground truth
when scoping a manager's approval rights, with no check that the username
actually matches what the ticket is about — a prompt-injected or
hallucinated redirect to a different real employee would let that
employee's actual manager approve it, believing it's correctly scoped to
their own report.

Fixed by flagging a clear mismatch warning into the Approval's `reasoning`
field (surfaced to reviewers via GET /approvals) when the planned username
doesn't appear anywhere in the ticket's own subject/body — not a hard
block, since ticket text is free-form and a mismatch doesn't always mean
something is wrong (see _username_appears_in_ticket_text's docstring).
"""

from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver

from app.agent import graph as graph_module
from app.agent.graph import _username_appears_in_ticket_text, compile_graph


def test_username_appears_in_ticket_text_true_for_matching_username():
    assert _username_appears_in_ticket_text("jsmith", "Offboard jsmith", "disable her account") is True


def test_username_appears_in_ticket_text_case_insensitive():
    assert _username_appears_in_ticket_text("JSmith", "offboard jsmith", "") is True


def test_username_appears_in_ticket_text_false_when_absent():
    assert _username_appears_in_ticket_text("jsmith", "Grant VPN access", "for the new contractor") is False


def test_username_appears_in_ticket_text_checks_both_subject_and_body():
    assert _username_appears_in_ticket_text("jsmith", "Offboarding request", "please disable jsmith") is True
    assert _username_appears_in_ticket_text("jsmith", "jsmith offboarding", "please disable this account") is True


class _FakeSession:
    pass


def _patch_plan_node_passthrough(monkeypatch, plan: list):
    async def _passthrough_classify_node(state):
        return {"category": "OFFBOARDING"}

    monkeypatch.setattr(graph_module, "classify_node", _passthrough_classify_node)

    async def _passthrough_plan_node(state):
        return {"plan": plan, "plan_index": 0}

    monkeypatch.setattr(graph_module, "plan_node", _passthrough_plan_node)


async def _run_await_approval_with_ticket(monkeypatch, tmp_path, ticket_subject, ticket_body, target_username):
    """Drives the real graph up to await_approval_node against an isolated
    on-disk DB (same pattern as test_fanout.py's mixed-plan test — the
    module-level engine/session-factory singletons are cached on first use
    and must be reset per test or writes would land in whatever DB a
    previous test already initialized)."""
    from app.config import get_settings
    from app.db import session as db_session_module

    db_path = tmp_path / "mismatch_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    try:
        await db_session_module.init_db()

        from app.db.models import Approval, Ticket, TicketStatus

        async with db_session_module.session_scope() as s:
            s.add(
                Ticket(
                    id=1, requester="hr@example.com", subject=ticket_subject,
                    body=ticket_body, status=TicketStatus.PLANNING,
                )
            )

        @asynccontextmanager
        async def _session():
            yield _FakeSession()

        monkeypatch.setattr(graph_module, "mcp_session", _session)

        plan = [{"tool": "disable_user", "args": {"username": target_username}, "reasoning": "offboard"}]
        _patch_plan_node_passthrough(monkeypatch, plan)
        graph = compile_graph(checkpointer=InMemorySaver())
        state = {
            "messages": [], "ticket_id": 1, "ticket_text": "irrelevant",
            "plan": [], "plan_index": 0, "pending_approval_id": None,
            "results": [], "done": False, "error": None,
        }
        config = {"configurable": {"thread_id": f"mismatch-test-{target_username}"}}
        await graph.ainvoke(state, config=config)

        async with db_session_module.session_scope() as s:
            approval = await s.get(Approval, 1)
            return approval.reasoning
    finally:
        db_session_module._engine = None
        db_session_module._session_factory = None
        get_settings.cache_clear()


async def test_await_approval_flags_mismatch_when_username_not_in_ticket_text(monkeypatch, tmp_path):
    reasoning = await _run_await_approval_with_ticket(
        monkeypatch, tmp_path,
        ticket_subject="Offboard employee", ticket_body="Please disable the departing contractor's account.",
        target_username="jsmith",
    )
    assert "TARGET MISMATCH" in reasoning
    assert "jsmith" in reasoning


async def test_await_approval_does_not_flag_when_username_matches_ticket_text(monkeypatch, tmp_path):
    reasoning = await _run_await_approval_with_ticket(
        monkeypatch, tmp_path,
        ticket_subject="Offboard jsmith", ticket_body="She left the company, please disable her account.",
        target_username="jsmith",
    )
    assert "TARGET MISMATCH" not in reasoning
    assert reasoning == "offboard"
