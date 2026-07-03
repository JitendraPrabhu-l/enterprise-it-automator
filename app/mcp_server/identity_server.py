"""Identity domain: employee lookup, onboarding, and offboarding.

Standalone FastMCP instance so this domain's tools could run as their own
process with its own deploy/scale profile, mirroring how a real identity
system (AD/Okta/IBM ID Management) would be a separate backend from
access-management or ticketing systems — see server.py, which composes this
alongside access_server and ticketing_server under one gateway process for
local/dev use via add_tool() namespacing.
"""

from mcp.server.fastmcp import FastMCP

from app.db.session import session_scope
from app.mcp_server import tools as t
from app.mcp_server.approval_gate import require_approval

identity_mcp = FastMCP("identity")


@identity_mcp.tool()
async def get_user(username: str) -> dict:
    """Look up an employee's identity record: status, department, and access grants."""
    async with session_scope() as session:
        return await t.get_user(session, username)


@identity_mcp.tool()
async def create_user(
    username: str, full_name: str, email: str, department: str = "", ticket_id: int | None = None
) -> dict:
    """Provision a new employee identity (onboarding). Not a sensitive action."""
    async with session_scope() as session:
        return await t.create_user(
            session, username, full_name, email, department, actor="mcp-client", ticket_id=ticket_id
        )


@identity_mcp.tool()
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
