"""Drift gate for app/mcp_server/tool_baseline.json — same shape as
tests/test_migrations.py's Alembic drift gate: regenerate the trusted
snapshot in-process from the live gateway and fail CI if it disagrees with
what's committed, catching a tool description/schema change that shipped
without a matching `python -m scripts.generate_tool_baseline` run.
"""

from app.agent.tool_integrity import compute_baseline, load_baseline
from app.mcp_server.server import _bootstrap, mcp


async def test_committed_baseline_matches_live_tool_set(monkeypatch):
    from app.config import get_settings
    from app.db import session as db_session_module

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    await _bootstrap()
    tools = await mcp.list_tools()
    tool_dicts = [
        {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
        for t in tools
    ]
    live = compute_baseline(tool_dicts)
    committed = load_baseline()

    assert live == committed, (
        "Live MCP tool set no longer matches app/mcp_server/tool_baseline.json — "
        "if this is an intentional tool change, regenerate the baseline with "
        "`python -m scripts.generate_tool_baseline` and commit it."
    )
