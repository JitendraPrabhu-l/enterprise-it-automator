import pytest
from sqlalchemy import select

from app.db.models import AuditLog
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


async def test_create_user_grants_executive_defaults(session):
    exec_user = await t.create_user(session, "cexec", "Casey Exec", "c@example.com", "Executive")
    assert exec_user["access_grants"] == ["vpn", "admin-panel", "netsuite", "workday"]


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


async def test_enable_user_reactivates_a_disabled_employee(session):
    """Re-onboarding: enable_user is the only real tool for bringing a
    previously-disabled employee back to active — added after a live bug
    where the LLM planner hallucinated a nonexistent identity_enable_user
    call for exactly this scenario, since no such tool existed yet."""
    await t.create_user(session, "ckent", "Clark Kent", "c@example.com")
    await t.disable_user(session, "ckent")

    enabled = await t.enable_user(session, "ckent")
    assert enabled["status"] == "active"

    fetched = await t.get_user(session, "ckent")
    assert fetched["status"] == "active"


async def test_enable_user_not_found(session):
    with pytest.raises(ToolError, match="No such user"):
        await t.enable_user(session, "ghost")


async def test_enable_user_already_active_rejected(session):
    await t.create_user(session, "ckent", "Clark Kent", "c@example.com")
    with pytest.raises(ToolError, match="already active"):
        await t.enable_user(session, "ckent")


async def test_revoke_access_not_granted_rejected(session):
    await t.create_user(session, "bwayne", "Bruce Wayne", "b@example.com")
    with pytest.raises(ToolError, match="does not have access"):
        await t.revoke_access(session, "bwayne", "github:engineering")


# --- Rejected attempts must leave an audit trail, not vanish silently ----
#
# Found live: a ticket's audit log showed only its follow-up
# ticketing_add_ticket_comment entry, with no record the agent had first
# attempted (and been refused) identity_disable_user against an
# already-disabled employee — every ToolError-raising rejection previously
# skipped _audit() entirely. Fixed by auditing (success=False) every
# rejection, same as a successful call is audited (success=True).


async def test_create_user_duplicate_rejection_is_audited(session):
    await t.create_user(session, "asmith", "Alice Smith", "asmith@example.com")
    with pytest.raises(ToolError):
        await t.create_user(session, "asmith", "Alice Smith 2", "a2@example.com")

    rows = list(await session.scalars(select(AuditLog).where(AuditLog.tool_name == "create_user")))
    rejections = [r for r in rows if not r.success]
    assert len(rejections) == 1
    assert "already exists" in rejections[0].result


async def test_disable_user_not_found_rejection_is_audited(session):
    with pytest.raises(ToolError):
        await t.disable_user(session, "ghost")

    rows = list(await session.scalars(select(AuditLog).where(AuditLog.tool_name == "disable_user")))
    assert len(rows) == 1
    assert rows[0].success is False
    assert "no such user" in rows[0].result.lower()


async def test_disable_user_already_disabled_rejection_is_audited(session):
    await t.create_user(session, "ckent", "Clark Kent", "c@example.com")
    await t.disable_user(session, "ckent")
    with pytest.raises(ToolError):
        await t.disable_user(session, "ckent")

    rows = list(await session.scalars(select(AuditLog).where(AuditLog.tool_name == "disable_user")))
    assert len(rows) == 2, "both the successful disable AND the rejected re-attempt must be audited"
    assert rows[0].success is True
    assert rows[1].success is False
    assert "already disabled" in rows[1].result


async def test_revoke_access_not_granted_rejection_is_audited(session):
    await t.create_user(session, "bwayne", "Bruce Wayne", "b@example.com")
    with pytest.raises(ToolError):
        await t.revoke_access(session, "bwayne", "github:engineering")

    rows = list(await session.scalars(select(AuditLog).where(AuditLog.tool_name == "revoke_access")))
    assert len(rows) == 1
    assert rows[0].success is False
    assert "does not have access" in rows[0].result


async def test_enable_user_not_found_rejection_is_audited(session):
    with pytest.raises(ToolError):
        await t.enable_user(session, "ghost")

    rows = list(await session.scalars(select(AuditLog).where(AuditLog.tool_name == "enable_user")))
    assert len(rows) == 1
    assert rows[0].success is False
    assert "no such user" in rows[0].result.lower()


async def test_enable_user_already_active_rejection_is_audited(session):
    await t.create_user(session, "ckent", "Clark Kent", "c@example.com")
    with pytest.raises(ToolError):
        await t.enable_user(session, "ckent")

    rows = list(await session.scalars(select(AuditLog).where(AuditLog.tool_name == "enable_user")))
    assert len(rows) == 1
    assert rows[0].success is False
    assert "already active" in rows[0].result


async def test_rejection_audit_row_survives_session_scope_rollback():
    """The actual bug caught before shipping: every one of these tools is
    invoked in production as `async with session_scope() as session: ...`
    (see identity_server.py/access_server.py), and session_scope() rolls
    back the WHOLE session on any exception leaving that block — including
    a session.add() that ran just before the raise. A first version of this
    fix added the audit row but never committed it, so it looked correct
    under the raw `session` fixture above (no wrapping rollback) but
    silently vanished under the real session_scope() codepath. This test
    uses session_scope() specifically to pin that it does NOT regress.
    """
    import os

    os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    from app.config import get_settings
    from app.db import session as db_session_module
    from app.db.session import init_db, session_scope

    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    await init_db()

    try:
        async with session_scope() as session:
            await t.create_user(session, "existing", "Existing User", "e@example.com")

        with pytest.raises(ToolError):
            async with session_scope() as session:
                await t.create_user(session, "existing", "Dup", "dup@example.com")

        async with session_scope() as session:
            rows = list(
                await session.scalars(
                    select(AuditLog).where(AuditLog.tool_name == "create_user", AuditLog.success.is_(False))
                )
            )
        assert len(rows) == 1, "the rejected attempt's audit row must survive session_scope's rollback"
    finally:
        db_session_module._engine = None
        db_session_module._session_factory = None
        get_settings.cache_clear()


async def test_is_sensitive(monkeypatch):
    """create_user/grant_access were added to the default sensitive set
    after a security review found they ran with zero human review — a
    prompt-injected or hallucinated planner output could otherwise
    provision access for the wrong real employee with nobody checking.

    Explicitly sets SENSITIVE_ACTIONS and clears get_settings' cache rather
    than relying on the .env default: other test modules (test_fanout.py,
    test_replanning.py) legitimately monkeypatch SENSITIVE_ACTIONS to pin
    their own scenarios, and since get_settings() is process-wide
    @lru_cache'd, whichever test runs first in a session determines what
    every later test sees unless each test that cares pins its own value.
    """
    from app.config import get_settings

    monkeypatch.setenv("SENSITIVE_ACTIONS", "disable_user,enable_user,revoke_access,create_user,grant_access")
    get_settings.cache_clear()

    assert t.is_sensitive("disable_user") is True
    assert t.is_sensitive("enable_user") is True
    assert t.is_sensitive("revoke_access") is True
    assert t.is_sensitive("grant_access") is True
    assert t.is_sensitive("create_user") is True
    assert t.is_sensitive("get_user") is False


async def test_is_sensitive_strips_domain_namespace(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("SENSITIVE_ACTIONS", "disable_user,revoke_access,create_user,grant_access")
    get_settings.cache_clear()

    assert t.is_sensitive("identity_disable_user") is True
    assert t.is_sensitive("access_revoke_access") is True
    assert t.is_sensitive("identity_create_user") is True
    assert t.is_sensitive("access_grant_access") is True
    assert t.is_sensitive("ticketing_add_ticket_comment") is False


async def test_audit_log_written_on_mutations(session):
    from sqlalchemy import select

    from app.db.models import AuditLog

    await t.create_user(session, "dprince", "Diana Prince", "d@example.com", ticket_id=42)
    await t.grant_access(session, "dprince", "vpn", ticket_id=42)

    rows = list(await session.scalars(select(AuditLog).order_by(AuditLog.id)))
    assert [r.tool_name for r in rows] == ["create_user", "grant_access"]
    assert all(r.ticket_id == 42 for r in rows)
    assert all(r.success for r in rows)


async def test_add_ticket_comment(session):
    from app.db.models import Ticket, TicketStatus

    ticket = Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.PLANNING)
    session.add(ticket)
    await session.flush()

    result = await t.add_ticket_comment(session, 1, "Working on it")
    assert result == {"ticket_id": 1, "comment": "Working on it"}
    assert "Working on it" in ticket.result_summary


async def test_add_ticket_comment_not_found(session):
    with pytest.raises(ToolError, match="No such ticket"):
        await t.add_ticket_comment(session, 999, "hello")


async def test_get_ticket_status(session):
    from app.db.models import Ticket, TicketStatus

    ticket = Ticket(id=2, requester="hr@x.com", subject="s", body="b", status=TicketStatus.COMPLETED)
    session.add(ticket)
    await session.flush()

    result = await t.get_ticket_status(session, 2)
    assert result["ticket_id"] == 2
    assert result["status"] == "completed"


async def test_get_ticket_status_not_found(session):
    with pytest.raises(ToolError, match="No such ticket"):
        await t.get_ticket_status(session, 999)
