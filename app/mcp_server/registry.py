"""Config-driven MCP server registry.

Today, all three domain servers (identity, access, ticketing) are composed
onto ONE gateway FastMCP process via add_tool() namespacing (see server.py)
— there is genuinely only one server to connect to. This registry exists so
that changes ONLY here (not in mcp_client.py or graph.py) are needed if a
domain's tools ever need to run as their own separate process with its own
deploy/scale profile (e.g. a real Jira/ServiceNow integration for the
ticketing domain that needs its own container and rate-limit budget — see
ROADMAP.md's Stage 2 "traps to avoid" section on why that's not built now).

Deliberately NOT an external service registry/gateway product (no
etcd/Consul, no IBM mcp-context-forge) — a small config mapping is the
right-sized version for a handful of domains that are still, in reality,
one process.
"""

from dataclasses import dataclass

from app.config import get_settings


@dataclass(frozen=True)
class ServerLocation:
    transport: str  # "stdio" | "http"
    url: str | None = None  # only used for http transport


def get_registry() -> dict[str, ServerLocation]:
    """Maps each tool-name prefix (identity_, access_, ticketing_) to where
    its tools are actually served. All three currently resolve to the same
    location (the one gateway process) — this indirection is what lets a
    future split change only this function's return value.
    """
    settings = get_settings()
    gateway = ServerLocation(transport=settings.mcp_transport, url=settings.mcp_server_url)
    return {
        "identity": gateway,
        "access": gateway,
        "ticketing": gateway,
    }


def resolve_domain_for_tool(tool_name: str) -> str:
    """Extracts the domain prefix from a namespaced tool name
    (identity_get_user -> "identity"). Falls back to "identity" for
    un-namespaced/legacy tool names (e.g. bare "get_user"), matching the
    gateway's own backward-compatible dispatch."""
    for domain in get_registry():
        if tool_name.startswith(f"{domain}_"):
            return domain
    return "identity"


def resolve_server_for_tool(tool_name: str) -> ServerLocation:
    """The location a given tool call should be routed to."""
    domain = resolve_domain_for_tool(tool_name)
    return get_registry()[domain]
