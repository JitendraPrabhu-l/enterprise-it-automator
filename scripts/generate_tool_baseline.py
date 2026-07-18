"""Regenerates app/mcp_server/tool_baseline.json from the gateway's actual,
currently-registered tool set.

Run this deliberately, once, after intentionally adding/removing/changing a
tool — review the resulting diff like any other code change, then commit it
alongside the tool change itself. tests/test_tool_baseline.py fails CI if
the live tool set and the committed baseline ever disagree without this
having been re-run, the same drift-gate shape as tests/test_migrations.py
for the Alembic chain.

Usage:
    python -m scripts.generate_tool_baseline
"""

import asyncio
import json
import os
import sys

from app.agent.tool_integrity import BASELINE_PATH, compute_baseline
from app.mcp_server.server import _bootstrap, mcp


async def _main() -> None:
    await _bootstrap()
    tools = await mcp.list_tools()
    tool_dicts = [
        {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
        for t in tools
    ]
    baseline = compute_baseline(tool_dicts)
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {len(baseline)} tool hashes to {BASELINE_PATH}")
    sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(_main())
    # asyncio.run() returns as soon as _main() completes, but the process
    # doesn't actually exit afterward — some dependency this module pulls in
    # (init_db()'s asyncpg/aiosqlite engine or an OTel exporter thread, not
    # narrowed down further since it's irrelevant to this one-shot script's
    # job) leaves a non-daemon thread alive. os._exit skips normal
    # interpreter teardown/atexit handling, which is fine here: the file is
    # already flushed to disk by the time we reach this line, and nothing
    # else needs graceful shutdown in a script whose only job just finished.
    os._exit(0)
