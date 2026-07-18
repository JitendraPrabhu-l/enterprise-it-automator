"""Tests for org-level cost governance (app/agent/token_budget.py's
check_client_org_budget / record_client_spend_and_check_budget) — the
per-ApiClient and org-wide DAILY token budgets layered on top of the
existing per-ticket MAX_TOKENS_PER_TICKET ceiling.
"""

import datetime as dt

import httpx
import pytest

from app.agent import token_budget
from app.config import get_settings
from app.db import session as db_session_module
from app.db.models import ApiClient, ApiClientRole, Ticket, TicketStatus
from app.db.session import session_scope


@pytest.fixture
async def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "client_token_budget_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    await db_session_module.init_db()
    yield
    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()
    token_budget.start_accounting(0)  # reset the ContextVar between tests


async def _make_client_and_ticket(name: str, submitted_by=None) -> tuple[int, int]:
    async with session_scope() as session:
        client = ApiClient(name=name, role=ApiClientRole.STANDARD, key=f"{name}-key")
        session.add(client)
        await session.flush()
        ticket = Ticket(
            requester="hr@x.com", subject="s", body="b", status=TicketStatus.PLANNING,
            submitted_by_client_id=client.id,
        )
        session.add(ticket)
        await session.flush()
        return client.id, ticket.id


async def test_check_returns_none_when_nothing_configured(monkeypatch, isolated_db):
    monkeypatch.delenv("MAX_TOKENS_PER_CLIENT_PER_DAY", raising=False)
    monkeypatch.delenv("MAX_ORG_TOKENS_PER_DAY", raising=False)
    get_settings.cache_clear()
    client_id, _ = await _make_client_and_ticket("c1")
    async with session_scope() as session:
        reason = await token_budget.check_client_org_budget(session, client_id)
    assert reason is None


async def test_check_flags_client_over_its_own_cap(monkeypatch, isolated_db):
    monkeypatch.setenv("MAX_TOKENS_PER_CLIENT_PER_DAY", "100")
    get_settings.cache_clear()
    client_id, _ = await _make_client_and_ticket("c1")

    async with session_scope() as session:
        client = await session.get(ApiClient, client_id)
        client.tokens_used_today = 150
        client.token_count_reset_at = dt.datetime.now(dt.timezone.utc)

    async with session_scope() as session:
        reason = await token_budget.check_client_org_budget(session, client_id)
    assert reason is not None
    assert "client" in reason.lower()


async def test_check_flags_org_total_over_cap_across_multiple_clients(monkeypatch, isolated_db):
    monkeypatch.setenv("MAX_ORG_TOKENS_PER_DAY", "100")
    get_settings.cache_clear()
    c1, _ = await _make_client_and_ticket("c1")
    c2, _ = await _make_client_and_ticket("c2")

    async with session_scope() as session:
        for cid, amount in [(c1, 60), (c2, 60)]:
            client = await session.get(ApiClient, cid)
            client.tokens_used_today = amount
            client.token_count_reset_at = dt.datetime.now(dt.timezone.utc)

    async with session_scope() as session:
        reason = await token_budget.check_client_org_budget(session, c1)
    assert reason is not None
    assert "org" in reason.lower()


async def test_record_spend_is_noop_outside_a_run(monkeypatch, isolated_db):
    monkeypatch.setenv("MAX_TOKENS_PER_CLIENT_PER_DAY", "100")
    get_settings.cache_clear()
    _, ticket_id = await _make_client_and_ticket("c1")
    token_budget._last_attributed.set(None)  # simulate "outside a run"

    reason = await token_budget.record_client_spend_and_check_budget(ticket_id)
    assert reason is None


async def test_record_spend_attributes_delta_to_the_tickets_client(monkeypatch, isolated_db):
    monkeypatch.setenv("MAX_TOKENS_PER_CLIENT_PER_DAY", "1000")
    get_settings.cache_clear()
    client_id, ticket_id = await _make_client_and_ticket("c1")

    token_budget.start_accounting(0)
    token_budget.add_tokens(75)
    reason = await token_budget.record_client_spend_and_check_budget(ticket_id)
    assert reason is None

    async with session_scope() as session:
        client = await session.get(ApiClient, client_id)
        assert client.tokens_used_today == 75


async def test_record_spend_does_not_double_count_across_two_calls(monkeypatch, isolated_db):
    monkeypatch.setenv("MAX_TOKENS_PER_CLIENT_PER_DAY", "1000")
    get_settings.cache_clear()
    client_id, ticket_id = await _make_client_and_ticket("c1")

    token_budget.start_accounting(0)
    token_budget.add_tokens(30)
    await token_budget.record_client_spend_and_check_budget(ticket_id)  # plan_node's call
    token_budget.add_tokens(20)
    await token_budget.record_client_spend_and_check_budget(ticket_id)  # replan_node's call

    async with session_scope() as session:
        client = await session.get(ApiClient, client_id)
        assert client.tokens_used_today == 50  # 30 + 20, not 30 + 50


async def test_record_spend_resets_counter_on_a_new_day(monkeypatch, isolated_db):
    monkeypatch.setenv("MAX_TOKENS_PER_CLIENT_PER_DAY", "1000")
    get_settings.cache_clear()
    client_id, ticket_id = await _make_client_and_ticket("c1")

    async with session_scope() as session:
        client = await session.get(ApiClient, client_id)
        client.tokens_used_today = 900
        client.token_count_reset_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)

    token_budget.start_accounting(0)
    token_budget.add_tokens(10)
    reason = await token_budget.record_client_spend_and_check_budget(ticket_id)
    assert reason is None  # would have tripped the cap if the stale 900 wasn't reset

    async with session_scope() as session:
        client = await session.get(ApiClient, client_id)
        assert client.tokens_used_today == 10


async def test_record_spend_reports_exceeded_once_delta_crosses_the_cap(monkeypatch, isolated_db):
    monkeypatch.setenv("MAX_TOKENS_PER_CLIENT_PER_DAY", "50")
    get_settings.cache_clear()
    client_id, ticket_id = await _make_client_and_ticket("c1")

    token_budget.start_accounting(0)
    token_budget.add_tokens(60)
    reason = await token_budget.record_client_spend_and_check_budget(ticket_id)
    assert reason is not None
    assert "client" in reason.lower()


# --- submission-time pre-check (POST /tickets) ------------------------------


@pytest.fixture
async def app_client(monkeypatch, tmp_path):
    db_path = tmp_path / "client_token_budget_api_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("API_KEY", "admin-bootstrap-key")
    monkeypatch.setenv("MAX_TOKENS_PER_CLIENT_PER_DAY", "100")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    import app.api.main as main_module

    await db_session_module.init_db()
    await main_module._ensure_bootstrap_admin_client()

    async with db_session_module.session_scope() as session:
        session.add(
            ApiClient(
                name="over-budget-client", role=ApiClientRole.STANDARD, key="over-budget-key",
                tokens_used_today=500, token_count_reset_at=dt.datetime.now(dt.timezone.utc),
            )
        )

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_submission_rejected_when_client_already_over_daily_token_cap(app_client):
    resp = await app_client.post(
        "/tickets",
        headers={"X-API-Key": "over-budget-key"},
        json={"requester": "hr@x.com", "subject": "s", "body": "Please grant rlee vpn access."},
    )
    assert resp.status_code == 429
    assert "token budget" in resp.json()["detail"].lower()
