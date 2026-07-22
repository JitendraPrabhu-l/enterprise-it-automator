"""Tests for the config-driven MCP server registry (Stage 2.3)."""

from app.mcp_server.registry import (
    ServerLocation,
    get_registry,
    resolve_domain_for_tool,
    resolve_server_for_tool,
)


def test_registry_has_all_four_domains():
    registry = get_registry()
    assert set(registry.keys()) == {"identity", "access", "app_access", "ticketing"}


def test_registry_all_domains_currently_point_at_same_gateway():
    """All four domains resolve to the same location today, since they're
    all composed onto one gateway process — this is expected, not a bug;
    the registry indirection exists for a FUTURE split, not a current one."""
    registry = get_registry()
    locations = list(registry.values())
    assert all(loc == locations[0] for loc in locations)


def test_resolve_domain_for_tool_identity():
    assert resolve_domain_for_tool("identity_get_user") == "identity"
    assert resolve_domain_for_tool("identity_disable_user") == "identity"


def test_resolve_domain_for_tool_access():
    assert resolve_domain_for_tool("access_grant_access") == "access"
    assert resolve_domain_for_tool("access_revoke_access") == "access"


def test_resolve_domain_for_tool_app_access():
    """app_access_* must resolve to "app_access", NOT be misdetected as
    "access" (the two domain names share a substring) — the real risk
    this app_access addition introduced to resolve_domain_for_tool's
    prefix-matching, worth pinning explicitly rather than trusting that
    startswith("access_") vs startswith("app_access_") never collides."""
    assert resolve_domain_for_tool("app_access_grant_app_access") == "app_access"
    assert resolve_domain_for_tool("app_access_revoke_app_access") == "app_access"
    assert resolve_domain_for_tool("app_access_list_app_access") == "app_access"


def test_resolve_domain_for_tool_ticketing():
    assert resolve_domain_for_tool("ticketing_add_ticket_comment") == "ticketing"


def test_resolve_domain_for_tool_unnamespaced_falls_back_to_identity():
    """Legacy/bare tool names (no domain prefix) fall back to identity,
    matching the gateway's own backward-compatible dispatch."""
    assert resolve_domain_for_tool("get_user") == "identity"
    assert resolve_domain_for_tool("some_other_bare_name") == "identity"


def test_resolve_server_for_tool_returns_a_server_location():
    location = resolve_server_for_tool("access_grant_access")
    assert isinstance(location, ServerLocation)
    assert location.transport in ("stdio", "http")


def test_server_location_defaults_to_stdio(monkeypatch):
    from app.config import get_settings

    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    get_settings.cache_clear()
    location = resolve_server_for_tool("identity_get_user")
    assert location.transport == "stdio"
    get_settings.cache_clear()


def test_server_location_reflects_http_transport_config(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_SERVER_URL", "http://example.invalid:9999/mcp")
    get_settings.cache_clear()
    location = resolve_server_for_tool("access_revoke_access")
    assert location.transport == "http"
    assert location.url == "http://example.invalid:9999/mcp"
    get_settings.cache_clear()
