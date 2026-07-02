import pytest

from app.mcp_server import tools as t
from app.mcp_server.tools import ToolError


async def test_create_and_get_user(session):
    created = await t.create_user(session, "asmith", "Alice Smith", "asmith@example.com", "Engineering")
    assert created["username"] == "asmith"
    assert created["status"] == "active"
    assert created["access_grants"] == ["vpn", "github:engineering", "jira:core-platform"]

    fetched = await t.get_user(session, "asmith")
    assert fetched["full_name"] == "Alice Smith"
    assert fetched["access_grants"] == ["vpn", "github:engineering", "jira:core-platform"]


async def test_create_user_grants_department_defaults(session):
    sales = await t.create_user(session, "rjones", "Raj Jones", "r@example.com", "Sales")
    assert sales["access_grants"] == ["vpn", "salesforce"]

    it_user = await t.create_user(session, "iuser", "IT User", "i@example.com", "IT")
    assert it_user["access_grants"] == ["vpn", "github:engineering", "admin-panel"]


async def test_create_user_unknown_department_gets_vpn_only(session):
    created = await t.create_user(session, "nuser", "New User", "n@example.com", "Marketing")
    assert created["access_grants"] == ["vpn"]


async def test_create_user_no_department_gets_vpn_only(session):
    created = await t.create_user(session, "nouser", "No Dept", "n2@example.com")
    assert created["access_grants"] == ["vpn"]


async def test_create_user_duplicate_rejected(session):
    await t.create_user(session, "asmith", "Alice Smith", "asmith@example.com")
    with pytest.raises(ToolError, match="already exists"):
        await t.create_user(session, "asmith", "Alice Smith 2", "a2@example.com")


async def test_get_user_not_found(session):
    with pytest.raises(ToolError, match="No such user"):
        await t.get_user(session, "ghost")


async def test_grant_and_revoke_access(session):
    await t.create_user(session, "bwayne", "Bruce Wayne", "b@example.com")  # no department -> ["vpn"]

    granted = await t.grant_access(session, "bwayne", "github:engineering")
    assert granted["access_grants"] == ["vpn", "github:engineering"]

    granted_again = await t.grant_access(session, "bwayne", "github:engineering")
    assert granted_again["access_grants"] == ["vpn", "github:engineering"], "must not duplicate grants"

    revoked = await t.revoke_access(session, "bwayne", "github:engineering")
    assert revoked["access_grants"] == ["vpn"]


async def test_disable_user(session):
    await t.create_user(session, "ckent", "Clark Kent", "c@example.com")
    disabled = await t.disable_user(session, "ckent")
    assert disabled["status"] == "disabled"

    fetched = await t.get_user(session, "ckent")
    assert fetched["status"] == "disabled"


async def test_disable_user_not_found(session):
    with pytest.raises(ToolError, match="No such user"):
        await t.disable_user(session, "ghost")


async def test_disable_user_already_disabled_rejected(session):
    await t.create_user(session, "ckent", "Clark Kent", "c@example.com")
    await t.disable_user(session, "ckent")
    with pytest.raises(ToolError, match="already disabled"):
        await t.disable_user(session, "ckent")


async def test_revoke_access_not_granted_rejected(session):
    await t.create_user(session, "bwayne", "Bruce Wayne", "b@example.com")
    with pytest.raises(ToolError, match="does not have access"):
        await t.revoke_access(session, "bwayne", "github:engineering")


async def test_is_sensitive():
    assert t.is_sensitive("disable_user") is True
    assert t.is_sensitive("revoke_access") is True
    assert t.is_sensitive("grant_access") is False
    assert t.is_sensitive("create_user") is False
    assert t.is_sensitive("get_user") is False


async def test_audit_log_written_on_mutations(session):
    from sqlalchemy import select

    from app.db.models import AuditLog

    await t.create_user(session, "dprince", "Diana Prince", "d@example.com", ticket_id=42)
    await t.grant_access(session, "dprince", "vpn", ticket_id=42)

    rows = list(await session.scalars(select(AuditLog).order_by(AuditLog.id)))
    assert [r.tool_name for r in rows] == ["create_user", "grant_access"]
    assert all(r.ticket_id == 42 for r in rows)
    assert all(r.success for r in rows)
