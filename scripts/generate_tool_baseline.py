"""Regenerates app/mcp_server/tool_baseline.json from the gateway's actual,
currently-registered tool set.

IMPORTANT — running this locally and committing the result is NOT the
supported workflow for the file that actually gets committed. hash_tool()
(app/agent/tool_integrity.py) hashes property names/required-set + full
description text, and while that's deliberately immune to per-property
JSON-Schema type-formatting differences across pydantic-core versions
(see that module's docstring for two confirmed-live rounds of that exact
bug), there is no guarantee some OTHER environment difference between a
dev machine and CI's locked/pinned environment won't again produce a
byte-different result for reasons that have nothing to do with an actual
tool change. Use `.github/workflows/regenerate-tool-baseline.yml`
(Actions tab → "Run workflow") instead after an intentional tool
change — it runs this exact script inside CI's own environment and
commits the result, which is the only way to GUARANTEE the committed file
matches what tests/test_tool_baseline.py's drift gate will itself compute.

Running this script locally is still useful as a quick, own-machine sanity
check ("did my tool change do what I expect") — just don't commit its
output directly.

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
