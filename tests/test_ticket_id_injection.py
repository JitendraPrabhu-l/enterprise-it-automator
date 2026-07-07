"""Regression test for a real bug found via live deployment verification:
_call_tool_for_ticket never injected ticket_id into a tool call's args, even
for tools whose MCP signature accepts it for audit-log attribution
(create_user/disable_user/grant_access/revoke_access). Confirmed live
against a real deployment's audit_log table: every row had ticket_id=NULL,
so GET /tickets/{id}/audit always returned an empty list even after
successful mutations — the audit entries existed, just unattributed to any
ticket.

Fixed in app/agent/graph.py's _call_tool_for_ticket, the single chokepoint
both execute_step_node and execute_batch_step_node route tool calls
through.
"""

from app.agent.graph import _call_tool_for_ticket
from app.mcp_server.tools import accepts_ticket_id, strip_domain_prefix


def test_accepts_ticket_id_true_for_mutating_identity_and_access_tools():
    assert accepts_ticket_id("create_user") is True
    assert accepts_ticket_id("disable_user") is True
    assert accepts_ticket_id("grant_access") is True
    assert accepts_ticket_id("revoke_access") is True


def test_accepts_ticket_id_true_for_ticketing_tools():
    """Regression test for a live bug: discover_tool_reference() surfaces
    ticketing_add_ticket_comment/get_ticket_status to the planner, and
    nothing stopped the LLM from spontaneously planning one even though no
    category prompt tells it to — when it did, the call failed FastMCP's
    own arg validation every time, since ticket_id is REQUIRED for both
    (unlike the optional param on the identity/access tools) and nothing
    supplied it."""
    assert accepts_ticket_id("add_ticket_comment") is True
    assert accepts_ticket_id("get_ticket_status") is True
    assert accepts_ticket_id("ticketing_add_ticket_comment") is True
    assert accepts_ticket_id("ticketing_get_ticket_status") is True


def test_accepts_ticket_id_true_for_namespaced_tool_names():
    """The LLM plans using the gateway's namespaced names
    (identity_create_user, not bare create_user) — the allowlist check must
    work against either form."""
    assert accepts_ticket_id("identity_create_user") is True
    assert accepts_ticket_id("identity_disable_user") is True
    assert accepts_ticket_id("access_grant_access") is True
    assert accepts_ticket_id("access_revoke_access") is True


def test_accepts_ticket_id_false_for_read_only_and_meta_tools():
    """get_user has no ticket_id parameter in its MCP signature — FastMCP
    rejects an unexpected kwarg, so injecting it unconditionally would break
    every read (this is why accepts_ticket_id must be an allowlist, not a
    blanket inject)."""
    assert accepts_ticket_id("get_user") is False
    assert accepts_ticket_id("identity_get_user") is False
    assert accepts_ticket_id("is_sensitive_action") is False


def test_strip_domain_prefix_removes_known_prefixes():
    assert strip_domain_prefix("identity_create_user") == "create_user"
    assert strip_domain_prefix("access_grant_access") == "grant_access"
    assert strip_domain_prefix("ticketing_add_ticket_comment") == "add_ticket_comment"


def test_strip_domain_prefix_leaves_bare_names_unchanged():
    assert strip_domain_prefix("create_user") == "create_user"


async def test_call_tool_for_ticket_injects_ticket_id_for_mutating_tool(monkeypatch):
    seen_args = {}

    class _FakeProxy:
        async def call_tool(self, tool, args):
            seen_args.update(args)
            return '{"status": "ok"}'

    monkeypatch.setattr("app.agent.graph.get_cached_proxy", lambda ticket_id: _FakeProxy())

    await _call_tool_for_ticket(42, "identity_create_user", {"username": "newuser"})

    assert seen_args == {"username": "newuser", "ticket_id": 42}


async def test_call_tool_for_ticket_does_not_inject_for_read_only_tool(monkeypatch):
    seen_args = {}

    class _FakeProxy:
        async def call_tool(self, tool, args):
            seen_args.update(args)
            return '{"username": "x"}'

    monkeypatch.setattr("app.agent.graph.get_cached_proxy", lambda ticket_id: _FakeProxy())

    await _call_tool_for_ticket(42, "identity_get_user", {"username": "x"})

    assert "ticket_id" not in seen_args


async def test_call_tool_for_ticket_injects_ticket_id_for_ticketing_tool(monkeypatch):
    """The exact live scenario: an offboarding ticket's plan included
    ticketing_add_ticket_comment with only {"comment": "..."} — no ticket_id
    (the LLM never plans it; it's hidden via _EXECUTOR_INJECTED_ARGS), which
    previously always failed FastMCP's arg validation since ticket_id is
    required for this tool."""
    seen_args = {}

    class _FakeProxy:
        async def call_tool(self, tool, args):
            seen_args.update(args)
            return '{"ok": true}'

    monkeypatch.setattr("app.agent.graph.get_cached_proxy", lambda ticket_id: _FakeProxy())

    await _call_tool_for_ticket(7, "ticketing_add_ticket_comment", {"comment": "offboarded"})

    assert seen_args == {"comment": "offboarded", "ticket_id": 7}


async def test_call_tool_for_ticket_does_not_override_an_explicit_ticket_id(monkeypatch):
    """If a caller already put ticket_id in args explicitly, the injection
    must not clobber it."""
    seen_args = {}

    class _FakeProxy:
        async def call_tool(self, tool, args):
            seen_args.update(args)
            return '{"status": "ok"}'

    monkeypatch.setattr("app.agent.graph.get_cached_proxy", lambda ticket_id: _FakeProxy())

    await _call_tool_for_ticket(42, "identity_create_user", {"username": "x", "ticket_id": 999})

    assert seen_args["ticket_id"] == 999
