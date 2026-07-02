import asyncio

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP

from app.config import Settings, get_settings
from app.mcp_server.tools import is_sensitive


def test_mcp_session_defaults_to_stdio(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    assert get_settings().mcp_transport == "stdio"
    get_settings.cache_clear()


def test_mcp_session_reads_http_transport_and_url(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_SERVER_URL", "http://example.invalid:9999/mcp")
    settings = get_settings()
    assert settings.mcp_transport == "http"
    assert settings.mcp_server_url == "http://example.invalid:9999/mcp"
    get_settings.cache_clear()


async def test_streamable_http_round_trip():
    """Spins up a minimal FastMCP server over streamable-HTTP on an ephemeral
    port and confirms a real client can connect, list tools, and call one —
    proving the remote-MCP-server code path actually works end to end, not
    just that config plumbing is correct. Uses a standalone FastMCP/tool (not
    app.mcp_server.server's mcp instance) to stay independent of DB setup.
    """
    test_mcp = FastMCP("test-server", host="127.0.0.1", port=8799)

    @test_mcp.tool()
    def is_sensitive_action(tool_name: str) -> bool:
        return is_sensitive(tool_name)

    config = uvicorn.Config(
        test_mcp.streamable_http_app(),
        host="127.0.0.1",
        port=8799,
        log_level="error",
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.05)

        async with streamable_http_client("http://127.0.0.1:8799/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert any(t.name == "is_sensitive_action" for t in tools.tools)

                result = await session.call_tool(
                    "is_sensitive_action", {"tool_name": "disable_user"}
                )
                assert result.isError is False
                text = next(b.text for b in result.content if hasattr(b, "text"))
                assert text == "true"
    finally:
        server.should_exit = True
        await server_task
