"""Tests for the app_access domain (app/mcp_server/tools.py's
grant_app_access/revoke_app_access/list_app_access) — real per-named-app
access state (Jira, Slack, Salesforce, GitHub, Workday, NetSuite, email),
distinct from the generic grant_access/revoke_access's free-text resource
list (see app/db/models.py's AppAccessGrant docstring for why the two
domains coexist).
"""

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import AppAccessStatus, AuditLog
from app.mcp_server import tools as t
from app.mcp_server.tools import ToolError


@pytest.fixture(autouse=True)
def _default_sensitive_actions(monkeypatch):
    # This dev machine's own .env may have a stale SENSITIVE_ACTIONS
    # value (pre-dating grant_app_access/revoke_app_access) — pin the
    # real app default explicitly so is_sensitive() tests below check
    # this codebase's actual intended default, not whatever happens to
    # be in a local .env file.
    monkeypatch.setenv(
        "SENSITIVE_ACTIONS",
        "disable_user,enable_user,revoke_access,create_user,grant_access,"
        "grant_app_access,revoke_app_access",
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_grant_app_access_creates_active_grant(session):
    await t.create_user(session, "asmith", "Alice Smith", "asmith@example.com")
    result = await t.grant_app_access(session, "asmith", "slack")
    assert result == {"username": "asmith", "app_name": "slack", "status": "active"}

    listed = await t.list_app_access(session, "asmith")
    assert listed == {"username": "asmith", "apps": ["slack"]}


async def test_grant_app_access_rejects_unknown_app(session):
    await t.create_user(session, "asmith", "Alice Smith", "asmith@example.com")
    with pytest.raises(ToolError, match="Unknown app"):
        await t.grant_app_access(session, "asmith", "not-a-real-app")


async def test_grant_app_access_rejects_unknown_user(session):
    with pytest.raises(ToolError, match="No such user"):
        await t.grant_app_access(session, "ghost", "slack")


async def test_grant_app_access_is_idempotent_by_active_state(session):
    """A second grant call while already active must not create a
    duplicate row — same dedup spirit as generic grant_access."""
    await t.create_user(session, "asmith", "Alice Smith", "asmith@example.com")
    await t.grant_app_access(session, "asmith", "slack")
    await t.grant_app_access(session, "asmith", "slack")

    from app.db.models import AppAccessGrant

    rows = list(
        await session.scalars(
            select(AppAccessGrant).where(
                AppAccessGrant.username == "asmith", AppAccessGrant.app_name == "slack"
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].status == AppAccessStatus.ACTIVE


async def test_revoke_app_access_flips_status(session):
    await t.create_user(session, "asmith", "Alice Smith", "asmith@example.com")
    await t.grant_app_access(session, "asmith", "jira")

    result = await t.revoke_app_access(session, "asmith", "jira")
    assert result == {"username": "asmith", "app_name": "jira", "status": "revoked"}

    listed = await t.list_app_access(session, "asmith")
    assert listed == {"username": "asmith", "apps": []}


async def test_revoke_app_access_without_active_grant_is_rejected(session):
    await t.create_user(session, "asmith", "Alice Smith", "asmith@example.com")
    with pytest.raises(ToolError, match="does not have active"):
        await t.revoke_app_access(session, "asmith", "jira")


async def test_revoke_app_access_rejection_is_audited(session):
    await t.create_user(session, "asmith", "Alice Smith", "asmith@example.com")
    with pytest.raises(ToolError):
        await t.revoke_app_access(session, "asmith", "jira")

    rows = list(
        await session.scalars(select(AuditLog).where(AuditLog.tool_name == "revoke_app_access"))
    )
    assert len(rows) == 1
    assert rows[0].success is False
    assert "does not have active" in rows[0].result


async def test_revoke_app_access_rejects_unknown_user(session):
    with pytest.raises(ToolError, match="No such user"):
        await t.revoke_app_access(session, "ghost", "slack")


async def test_grant_then_revoke_then_regrant_preserves_history(session):
    """A grant-revoke-regrant cycle must produce a SEPARATE new row, not
    reuse/resurrect the old one — the whole reason AppAccessGrant is a
    row-per-event table instead of a single mutable state column (see its
    docstring)."""
    await t.create_user(session, "asmith", "Alice Smith", "asmith@example.com")
    await t.grant_app_access(session, "asmith", "github")
    await t.revoke_app_access(session, "asmith", "github")
    await t.grant_app_access(session, "asmith", "github")

    from app.db.models import AppAccessGrant

    rows = list(
        await session.scalars(
            select(AppAccessGrant)
            .where(AppAccessGrant.username == "asmith", AppAccessGrant.app_name == "github")
            .order_by(AppAccessGrant.id)
        )
    )
    assert len(rows) == 2
    assert rows[0].status == AppAccessStatus.REVOKED
    assert rows[1].status == AppAccessStatus.ACTIVE

    listed = await t.list_app_access(session, "asmith")
    assert listed == {"username": "asmith", "apps": ["github"]}


async def test_list_app_access_rejects_unknown_user(session):
    with pytest.raises(ToolError, match="No such user"):
        await t.list_app_access(session, "ghost")


async def test_list_app_access_sorted_and_active_only(session):
    await t.create_user(session, "asmith", "Alice Smith", "asmith@example.com")
    await t.grant_app_access(session, "asmith", "slack")
    await t.grant_app_access(session, "asmith", "jira")
    await t.grant_app_access(session, "asmith", "github")
    await t.revoke_app_access(session, "asmith", "jira")

    listed = await t.list_app_access(session, "asmith")
    assert listed == {"username": "asmith", "apps": ["github", "slack"]}


async def test_grant_app_access_is_sensitive():
    assert t.is_sensitive("grant_app_access")
    assert t.is_sensitive("app_access_grant_app_access")


async def test_revoke_app_access_is_sensitive():
    assert t.is_sensitive("revoke_app_access")
    assert t.is_sensitive("app_access_revoke_app_access")


async def test_list_app_access_is_not_sensitive():
    assert not t.is_sensitive("list_app_access")


async def test_app_access_tools_accept_ticket_id():
    assert t.accepts_ticket_id("grant_app_access")
    assert t.accepts_ticket_id("revoke_app_access")
    assert not t.accepts_ticket_id("list_app_access")
