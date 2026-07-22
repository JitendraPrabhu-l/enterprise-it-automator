"""Tests for the gateway server's domain-server composition (Stage 2.1).

The gateway composes four independently-defined FastMCP domain servers
(identity, access, app_access, ticketing) onto one process via add_tool()
namespacing — verifies the composition itself produces the right tool set
with the right names, independent of any real tool-call behavior (covered
by test_mcp_transport.py's real-subprocess tests and test_tools.py's
direct tool tests).
"""

from app.mcp_server.server import _bootstrap, mcp


async def test_gateway_exposes_all_domain_tools_namespaced(monkeypatch):
    from app.db import session as db_session_module
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    await _bootstrap()
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}

    assert "identity_get_user" in tool_names
    assert "identity_create_user" in tool_names
    assert "identity_disable_user" in tool_names
    assert "access_grant_access" in tool_names
    assert "access_revoke_access" in tool_names
    assert "app_access_grant_app_access" in tool_names
    assert "app_access_revoke_app_access" in tool_names
    assert "app_access_list_app_access" in tool_names
    assert "ticketing_add_ticket_comment" in tool_names
    assert "ticketing_get_ticket_status" in tool_names
    assert "is_sensitive_action" in tool_names

    # No bare (un-namespaced) tool names should leak through — every
    # domain tool must be reachable only via its namespaced form.
    assert "get_user" not in tool_names
    assert "disable_user" not in tool_names
    assert "grant_access" not in tool_names
    assert "revoke_access" not in tool_names
    assert "grant_app_access" not in tool_names
    assert "revoke_app_access" not in tool_names


async def test_gateway_exposes_exactly_12_tools(monkeypatch):
    """4 identity (get_user/create_user/disable_user/enable_user) + 2 access
    + 3 app_access (grant_app_access/revoke_app_access/list_app_access)
    + 2 ticketing + 1 is_sensitive_action = 12. A regression here means a
    domain server's tool count changed without updating this expectation,
    or composition silently dropped/duplicated a tool.

    enable_user (re-activating a previously disabled employee) was added
    after a live bug: an onboarding ticket for an employee who exists but
    is disabled had no real tool to reach for, and the LLM planner
    hallucinated a nonexistent identity_enable_user call instead of
    failing cleanly."""
    from app.db import session as db_session_module
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    await _bootstrap()
    tools = await mcp.list_tools()
    assert len(tools) == 12


async def test_gateway_tools_preserve_original_descriptions(monkeypatch):
    from app.db import session as db_session_module
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    await _bootstrap()
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}

    assert "employee's identity record" in by_name["identity_get_user"].description
    assert "Grant an employee access" in by_name["access_grant_access"].description


async def test_gateway_tools_preserve_annotations(monkeypatch):
    """MCP spec 2025-11-25's tool annotations (readOnlyHint/destructiveHint/
    idempotentHint/openWorldHint) must survive the gateway's re-registration
    (server.py's _compose_gateway), not just name/description — a client
    relying on these hints to decide, e.g., whether a tool call needs extra
    confirmation would otherwise see none at all on any composed tool.
    """
    from app.db import session as db_session_module
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    await _bootstrap()
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}

    read_only = by_name["identity_get_user"].annotations
    assert read_only.readOnlyHint is True
    assert read_only.destructiveHint is False
    assert read_only.idempotentHint is True

    sensitive_destructive = by_name["identity_disable_user"].annotations
    assert sensitive_destructive.readOnlyHint is False
    assert sensitive_destructive.destructiveHint is True
    assert sensitive_destructive.idempotentHint is False

    idempotent_write = by_name["access_grant_access"].annotations
    assert idempotent_write.readOnlyHint is False
    assert idempotent_write.destructiveHint is False
    assert idempotent_write.idempotentHint is True

    not_idempotent_write = by_name["ticketing_add_ticket_comment"].annotations
    assert not_idempotent_write.idempotentHint is False

    meta_tool = by_name["is_sensitive_action"].annotations
    assert meta_tool.readOnlyHint is True

    app_access_read = by_name["app_access_list_app_access"].annotations
    assert app_access_read.readOnlyHint is True

    app_access_grant = by_name["app_access_grant_app_access"].annotations
    assert app_access_grant.destructiveHint is False
    assert app_access_grant.idempotentHint is True

    app_access_revoke = by_name["app_access_revoke_app_access"].annotations
    assert app_access_revoke.destructiveHint is True
    assert app_access_revoke.idempotentHint is False
