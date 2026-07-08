"""Identity domain: employee lookup, onboarding, and offboarding.

Standalone FastMCP instance so this domain's tools could run as their own
process with its own deploy/scale profile, mirroring how a real identity
system (AD/Okta/IBM ID Management) would be a separate backend from
access-management or ticketing systems — see server.py, which composes this
alongside access_server and ticketing_server under one gateway process for
local/dev use via add_tool() namespacing.
"""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.db.session import session_scope
from app.mcp_server import tools as t
from app.mcp_server.approval_gate import require_approval

identity_mcp = FastMCP("identity")


@identity_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def get_user(username: str) -> dict:
    """Look up an employee's identity record: status, department, and access grants."""
    async with session_scope() as session:
        return await t.get_user(session, username)


@identity_mcp.tool(
    # Not idempotent: a second call with the same username fails with
    # "User already exists" (tools.py's create_user) rather than being a
    # no-op success — per the annotation's own definition ("calling the
    # tool repeatedly... will have no ADDITIONAL EFFECT"), a repeat call
    # here has a DIFFERENT effect (an error, not a silent success), so this
    # is correctly idempotentHint=False.
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
async def create_user(
    username: str, full_name: str, email: str, department: str = "", ticket_id: int | None = None
) -> dict:
    """Provision a new employee identity (onboarding). Not a sensitive action."""
    async with session_scope() as session:
        return await t.create_user(
            session, username, full_name, email, department, actor="mcp-client", ticket_id=ticket_id
        )


@identity_mcp.tool(
    # destructiveHint=True: disabling an account is a real, consequential
    # state change (this is also the SENSITIVE tool requiring approval —
    # see require_approval below). Not idempotentHint: a second call
    # against an already-disabled user fails with "already disabled"
    # rather than succeeding as a no-op, so a repeat call has a different
    # observable effect (an error) than the first.
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
async def disable_user(
    username: str, approval_id: int, ticket_id: int | None = None
) -> dict:
    """Disable an employee's account (offboarding). SENSITIVE: requires a prior
    human-approved `approval_id` (see request_approval / the FastAPI /approvals
    endpoints) matching this exact tool call, or the server refuses the action.

    Checks the approval against "identity_disable_user" (this tool's namespaced
    gateway name, not the bare "disable_user") — the agent plans and the
    Approval row is created using the namespaced name the LLM actually chose
    (see server.py's add_tool() composition), so the approval-gate check must
    match that exact string or every sensitive call fails with a tool-name
    mismatch even when correctly approved.
    """
    async with session_scope() as session:
        await require_approval(
            session, approval_id, "identity_disable_user", {"username": username}
        )
        return await t.disable_user(
            session, username, actor="mcp-client", ticket_id=ticket_id
        )


@identity_mcp.tool(
    # Same sensitivity/annotation reasoning as disable_user: re-activating a
    # previously offboarded account is just as consequential a state change
    # as disabling one, not a routine onboarding step, so it requires the
    # same HITL approval gate.
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
async def enable_user(
    username: str, approval_id: int, ticket_id: int | None = None
) -> dict:
    """Re-activate a previously disabled employee's account (re-onboarding).
    SENSITIVE: requires a prior human-approved `approval_id` matching this
    exact tool call, or the server refuses the action — see disable_user's
    docstring for why the approval-gate check uses the namespaced
    "identity_enable_user" name.

    Added after a live bug: an onboarding ticket for an employee who
    already exists but is disabled had no real tool to reach for, and the
    LLM planner hallucinated a nonexistent identity_enable_user call
    instead of failing cleanly. This is that tool now.
    """
    async with session_scope() as session:
        await require_approval(
            session, approval_id, "identity_enable_user", {"username": username}
        )
        return await t.enable_user(
            session, username, actor="mcp-client", ticket_id=ticket_id
        )
