"""Tests for POST /telegram/webhook — account linking via /start and
deciding approvals via inline-button callback_query updates.

All outbound Telegram API calls (answerCallbackQuery, sendMessage) are
monkeypatched to no-ops, since these tests are about OUR authorization and
state-transition logic, not Telegram's API. The one thing that must NEVER
be mocked away is _decide_approval_core itself — every test here exercises
the real thing, since the entire point of this feature is that Telegram is
just another authenticated entry point into the SAME approval logic the
dashboard uses, never a weaker parallel path.
"""

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import session as db_session_module


@pytest.fixture
async def app_client(monkeypatch, tmp_path):
    db_path = tmp_path / "telegram_webhook_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("API_KEY", "admin-bootstrap-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-telegram-token")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    import app.api.main as main_module
    from app.notifications import telegram as telegram_module

    await db_session_module.init_db()
    await main_module._ensure_bootstrap_admin_client()

    # Every outbound Telegram API call is a no-op in these tests — they
    # exercise OUR webhook logic, not Telegram's actual HTTP API.
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(telegram_module, "send_decision_confirmation", _noop)
    monkeypatch.setattr(telegram_module, "answer_callback_query", _noop)

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, main_module

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def _seed_reviewer(username: str, role, token: str) -> None:
    from app.db.models import Reviewer

    async with db_session_module.session_scope() as session:
        session.add(Reviewer(username=username, role=role, token=token))


async def _seed_pending_approval(*, tool_name: str = "disable_user", username: str = "jsmith") -> int:
    from app.db.models import Approval, ApprovalStatus, Ticket, TicketStatus

    async with db_session_module.session_scope() as session:
        ticket = Ticket(
            requester="hr@example.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL
        )
        session.add(ticket)
        await session.flush()
        approval = Approval(
            ticket_id=ticket.id, tool_name=tool_name, tool_args={"username": username},
            status=ApprovalStatus.PENDING,
        )
        session.add(approval)
        await session.flush()
        return approval.id


# --- linking via /start ---------------------------------------------------


async def test_start_links_chat_to_reviewer_with_valid_token(app_client):
    ac, _ = app_client
    from app.db.models import Reviewer, ReviewerRole

    await _seed_reviewer("mchen", ReviewerRole.MANAGER, "real-reviewer-token")

    resp = await ac.post(
        "/telegram/webhook",
        json={"message": {"text": "/start real-reviewer-token", "chat": {"id": 555}}},
    )
    assert resp.status_code == 200

    async with db_session_module.session_scope() as session:
        reviewer = await session.scalar(select(Reviewer).where(Reviewer.username == "mchen"))
    assert reviewer.telegram_chat_id == "555"


async def test_start_with_wrong_token_links_nothing(app_client):
    ac, _ = app_client
    from app.db.models import Reviewer, ReviewerRole

    await _seed_reviewer("mchen", ReviewerRole.MANAGER, "real-reviewer-token")

    resp = await ac.post(
        "/telegram/webhook",
        json={"message": {"text": "/start totally-wrong-token", "chat": {"id": 555}}},
    )
    assert resp.status_code == 200

    async with db_session_module.session_scope() as session:
        reviewer = await session.scalar(select(Reviewer).where(Reviewer.username == "mchen"))
    assert reviewer.telegram_chat_id is None


async def test_start_with_no_token_links_nothing(app_client):
    ac, _ = app_client
    resp = await ac.post("/telegram/webhook", json={"message": {"text": "/start", "chat": {"id": 555}}})
    assert resp.status_code == 200  # must not crash on a bare /start with nothing after it


# --- deciding via callback_query -------------------------------------------


async def test_callback_query_approve_from_linked_it_admin_succeeds(app_client, monkeypatch):
    ac, main_module = app_client
    from app.db.models import Approval, ApprovalStatus, ReviewerRole
    from app.notifications.telegram import _decision_callback_data

    await _seed_reviewer("admin", ReviewerRole.IT_ADMIN, "admin-token")
    # Link the chat directly (skip the /start round trip, already covered above).
    from app.db.models import Reviewer

    async with db_session_module.session_scope() as session:
        row = await session.scalar(select(Reviewer).where(Reviewer.username == "admin"))
        row.telegram_chat_id = "999"

    approval_id = await _seed_pending_approval()

    async def _fake_resume(ticket_id):
        return {
            "ticket_id": ticket_id, "done": True, "plan": [], "results": [],
            "error": None, "interrupted": False, "pending_approval": None,
        }

    monkeypatch.setattr(main_module, "resume_ticket_run", _fake_resume)

    resp = await ac.post(
        "/telegram/webhook",
        json={
            "callback_query": {
                "id": "cbq-1",
                "data": _decision_callback_data(approval_id, True),
                "message": {"chat": {"id": 999}},
            }
        },
    )
    assert resp.status_code == 200

    async with db_session_module.session_scope() as session:
        approval = await session.get(Approval, approval_id)
        assert approval.status == ApprovalStatus.APPROVED
        assert approval.reviewer == "admin"


async def test_callback_query_reject_from_linked_reviewer_succeeds(app_client):
    ac, _ = app_client
    from app.db.models import Approval, ApprovalStatus, Reviewer, ReviewerRole
    from app.notifications.telegram import _decision_callback_data

    await _seed_reviewer("admin", ReviewerRole.IT_ADMIN, "admin-token")
    async with db_session_module.session_scope() as session:
        row = await session.scalar(select(Reviewer).where(Reviewer.username == "admin"))
        row.telegram_chat_id = "999"

    approval_id = await _seed_pending_approval()

    resp = await ac.post(
        "/telegram/webhook",
        json={
            "callback_query": {
                "id": "cbq-2",
                "data": _decision_callback_data(approval_id, False),
                "message": {"chat": {"id": 999}},
            }
        },
    )
    assert resp.status_code == 200

    async with db_session_module.session_scope() as session:
        approval = await session.get(Approval, approval_id)
        assert approval.status == ApprovalStatus.REJECTED


async def test_callback_query_from_unlinked_chat_decides_nothing(app_client):
    """The core security property: a chat that was never linked to any
    reviewer (i.e. never proved it holds a real reviewer token via /start)
    must not be able to decide ANY approval, no matter what callback_data
    it sends."""
    ac, _ = app_client
    from app.db.models import Approval, ApprovalStatus
    from app.notifications.telegram import _decision_callback_data

    approval_id = await _seed_pending_approval()

    resp = await ac.post(
        "/telegram/webhook",
        json={
            "callback_query": {
                "id": "cbq-3",
                "data": _decision_callback_data(approval_id, True),
                "message": {"chat": {"id": 424242}},  # never linked to anyone
            }
        },
    )
    assert resp.status_code == 200  # webhook always 200s to Telegram — failure is reported via answerCallbackQuery

    async with db_session_module.session_scope() as session:
        approval = await session.get(Approval, approval_id)
        assert approval.status == ApprovalStatus.PENDING, "must remain undecided"


async def test_callback_query_manager_cannot_decide_unrelated_approval(app_client):
    """A linked MANAGER reviewer must still go through the real RBAC check
    (app/api/rbac.py) — Telegram must never be a way to bypass "only the
    target employee's own manager may decide this.\""""
    ac, _ = app_client
    from app.db.models import Approval, ApprovalStatus, Reviewer, ReviewerRole
    from app.notifications.telegram import _decision_callback_data

    await _seed_reviewer("mchen", ReviewerRole.MANAGER, "mchen-token")
    async with db_session_module.session_scope() as session:
        row = await session.scalar(select(Reviewer).where(Reviewer.username == "mchen"))
        row.telegram_chat_id = "777"

    # jsmith's manager_username is never set to "mchen" here — no EmployeeUser
    # row exists at all for jsmith, so mchen is not entitled to decide this.
    approval_id = await _seed_pending_approval(username="jsmith")

    resp = await ac.post(
        "/telegram/webhook",
        json={
            "callback_query": {
                "id": "cbq-4",
                "data": _decision_callback_data(approval_id, True),
                "message": {"chat": {"id": 777}},
            }
        },
    )
    assert resp.status_code == 200

    async with db_session_module.session_scope() as session:
        approval = await session.get(Approval, approval_id)
        assert approval.status == ApprovalStatus.PENDING, "an unauthorized manager must not decide it"


async def test_callback_query_cannot_decide_an_already_decided_approval_twice(app_client):
    ac, _ = app_client
    from app.db.models import Approval, ApprovalStatus, Reviewer, ReviewerRole
    from app.notifications.telegram import _decision_callback_data

    await _seed_reviewer("admin", ReviewerRole.IT_ADMIN, "admin-token")
    async with db_session_module.session_scope() as session:
        row = await session.scalar(select(Reviewer).where(Reviewer.username == "admin"))
        row.telegram_chat_id = "999"

    approval_id = await _seed_pending_approval()
    async with db_session_module.session_scope() as session:
        approval = await session.get(Approval, approval_id)
        approval.status = ApprovalStatus.APPROVED
        approval.reviewer = "someone-else"

    resp = await ac.post(
        "/telegram/webhook",
        json={
            "callback_query": {
                "id": "cbq-5",
                "data": _decision_callback_data(approval_id, False),
                "message": {"chat": {"id": 999}},
            }
        },
    )
    assert resp.status_code == 200

    async with db_session_module.session_scope() as session:
        approval = await session.get(Approval, approval_id)
        assert approval.reviewer == "someone-else", "must not be overwritten by the late Telegram decision"


async def test_webhook_rejects_wrong_secret_token(app_client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "the-real-secret")
    get_settings.cache_clear()
    ac, _ = app_client

    resp = await ac.post(
        "/telegram/webhook",
        json={"message": {"text": "/start x", "chat": {"id": 1}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )
    assert resp.status_code == 401
    get_settings.cache_clear()


async def test_webhook_accepts_correct_secret_token(app_client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "the-real-secret")
    get_settings.cache_clear()
    ac, _ = app_client

    resp = await ac.post(
        "/telegram/webhook",
        json={"message": {"text": "/start x", "chat": {"id": 1}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "the-real-secret"},
    )
    assert resp.status_code == 200
    get_settings.cache_clear()


async def test_webhook_is_noop_when_telegram_bot_token_unset(monkeypatch, tmp_path):
    db_path = tmp_path / "telegram_webhook_unset_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("API_KEY", "admin-bootstrap-key")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    import app.api.main as main_module

    await db_session_module.init_db()

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/telegram/webhook",
            json={"message": {"text": "/start anything", "chat": {"id": 1}}},
        )
    assert resp.status_code == 200

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()
