"""Tool-description integrity check — the defense against "tool poisoning."

`discover_tool_reference()` (app/agent/graph.py) re-pulls `tools/list` from
the MCP gateway on every plan/replan and feeds whatever comes back straight
into the planner's system prompt. That live-discovery design is the correct
implementation of MCP's actual discovery phase, but it's also exactly the
mechanism a tool-poisoning attack exploits: a compromised or MITM'd server
(only reachable in the first place over the HTTP transport — see
app/mcp_server/server.py's _BearerTokenMiddleware for that boundary) could
mutate a tool's description between sessions to steer the planner, with no
code diff for a human to ever review.

This module hashes each discovered tool's (name, description, input_schema)
against a committed baseline (app/mcp_server/tool_baseline.json, regenerated
deliberately via scripts/generate_tool_baseline.py after an intentional tool
change) and reports any drift. Detection only, by default — a mismatch is
logged and counted (metrics.MCP_TOOL_BASELINE_MISMATCH) but does not stop a
ticket, since a deployment that just shipped a real tool change and forgot
to regenerate the baseline shouldn't have every ticket start failing.
Settings.tool_integrity_strict=True (opt-in) makes a mismatch a hard,
FAILED-ticket error instead — the fail-closed choice for a deployment that
wants "an unreviewed tool change halts all agent activity" over "keep
running against a tool set nobody re-approved."
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASELINE_PATH = Path(__file__).resolve().parent.parent / "mcp_server" / "tool_baseline.json"


class ToolIntegrityError(RuntimeError):
    """Raised when discovered tools drift from the committed baseline and
    Settings.tool_integrity_strict is enabled."""


def hash_tool(name: str, description: str | None, input_schema: dict[str, Any] | None) -> str:
    """Stable hash over the parts of a tool definition that matter to the
    planner: what it's called, what it claims to do, and what arguments it
    accepts. json.dumps(sort_keys=True) makes this independent of dict
    insertion order (the mcp SDK doesn't guarantee one) so re-hashing the
    same effective tool definition always reproduces the same hash.
    """
    payload = json.dumps(
        {"name": name, "description": description or "", "input_schema": input_schema or {}},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_baseline(tools: list[dict[str, Any]]) -> dict[str, str]:
    return {t["name"]: hash_tool(t["name"], t.get("description"), t.get("input_schema")) for t in tools}


def load_baseline() -> dict[str, str]:
    """Empty dict (not an error) when the baseline file doesn't exist yet —
    e.g. a fresh checkout before the first `python -m scripts.generate_tool_baseline`
    run. check_tool_integrity then reports every discovered tool as "not in
    baseline," which is the correct, honest signal: nothing has been
    trusted yet.
    """
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def check_tool_integrity(tools: list[dict[str, Any]]) -> list[str]:
    """Returns a list of human-readable mismatch descriptions — empty when
    every discovered tool's hash matches the committed baseline exactly and
    no baseline entry is missing from discovery. Compares the FULL
    discovered tool set (including meta-tools like is_sensitive_action),
    not just the ones discover_tool_reference() surfaces to the planner
    prompt — a tampered tool that never reaches the prompt text is still a
    tampered tool.
    """
    baseline = load_baseline()
    current = compute_baseline(tools)

    mismatches = []
    for name, current_hash in current.items():
        if name not in baseline:
            mismatches.append(f"new tool not in baseline: {name!r}")
        elif baseline[name] != current_hash:
            mismatches.append(f"tool description/schema changed since baseline: {name!r}")
    for name in baseline:
        if name not in current:
            mismatches.append(f"tool present in baseline but missing from live discovery: {name!r}")
    return mismatches
