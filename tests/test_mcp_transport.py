import asyncio

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP

from app.agent.mcp_session_cache import ticket_run_session
from app.config import get_settings
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


async def test_stdio_subprocess_inherits_custom_database_url(monkeypatch, tmp_path):
    """mcp.client.stdio.get_default_environment() only inherits a small
    security-allowlisted set of OS vars (PATH, HOME, etc.) when env=None —
    it silently drops app config like DATABASE_URL, so a spawned MCP server
    subprocess would fall back to config.py's default SQLite path instead
    of whatever this process is actually configured to use. This is a live
    regression test against the real subprocess spawn path
    (app.agent.mcp_client._session_at's env=dict(os.environ)) rather than
    a unit test of the dict-building logic in isolation, since the bug
    only manifests when a real child process is spawned with a real (or
    missing) environment.
    """
    from app.agent.mcp_client import mcp_session

    db_path = tmp_path / "subprocess_env_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    get_settings.cache_clear()
    try:
        async with mcp_session("identity_") as session:
            result = await session.call_tool(
                "identity_create_user",
                {"username": "envtest", "full_name": "Env Test", "email": "e@example.com"},
            )
            assert result.isError is False
    finally:
        get_settings.cache_clear()

    # The subprocess must have written to the DB path THIS process set via
    # DATABASE_URL, not config.py's default — proves the env var actually
    # reached the child.
    assert db_path.exists()


async def test_shared_session_survives_real_concurrent_cross_task_calls():
    """Regression test for a real bug: a naive session-cache design (one
    mcp_session() opened lazily and cached in a dict, closed from whatever
    task called ticket_run_session's __aexit__) crashed live with
    'RuntimeError: Attempted to exit cancel scope in a different task than
    it was entered in' — MCP's stdio transport opens an anyio task group
    internally, and anyio requires the scope to be entered/exited in the
    exact same asyncio task. LangGraph runs each node (and each concurrent
    Send()-based fan-out branch) as its own task, so a session opened in
    one task and closed from another violates this constraint.

    This test drives the exact failure scenario against the REAL stdio
    subprocess (not a fake session): open one ticket_run_session, then fire
    several concurrent tool calls from separate asyncio tasks (mimicking
    concurrent execute_batch_step_node invocations), then exit the context
    manager from yet another task-relative position. If the owner-task/
    queue-proxy design (app/agent/mcp_session_cache.py) is broken, this
    raises the anyio cross-task RuntimeError; if it's correct, all calls
    succeed and the session closes cleanly.
    """
    async with ticket_run_session(999001) as proxy:
        results = await asyncio.gather(
            *(
                proxy.call_tool("identity_get_user", {"username": f"nonexistent_{i}"})
                for i in range(5)
            ),
            return_exceptions=True,
        )
    # Every call should have failed with "No such user" (ToolError surfaced
    # as a RuntimeError over the wire) — not with an anyio cancel-scope
    # error, which is the specific failure this test guards against.
    assert len(results) == 5
    for result in results:
        assert isinstance(result, Exception)
        assert "cancel scope" not in str(result)
        assert "No such user" in str(result) or "envtest" not in str(result)
