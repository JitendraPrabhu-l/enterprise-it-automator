"""Access domain: granting and revoking resource access for employees.

Standalone FastMCP instance so this domain's tools could run as their own
process with its own deploy/scale profile, separate from identity
management or ticketing — see server.py, which composes this alongside
identity_server and ticketing_server under one gateway process for
local/dev use via add_tool() namespacing.
"""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.db.session import session_scope
from app.mcp_server import tools as t
from app.mcp_server.approval_gate import require_approval

access_mcp = FastMCP("access")


@access_mcp.tool(
    # idempotentHint=True: tools.py's grant_access dedups
    # ("if resource not in user.access_grants") — a repeat call with the
    # same args is a genuine no-op, unlike create_user/disable_user above.
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def grant_access(
    username: str, resource: str, ticket_id: int | None = None
) -> dict:
    """Grant an employee access to a resource (e.g. 'github:engineering'). Not sensitive."""
    async with session_scope() as session:
        return await t.grant_access(
            session, username, resource, actor="mcp-client", ticket_id=ticket_id
        )


@access_mcp.tool(
    # destructiveHint=True: removing access is a real, consequential state
    # change (this is also the SENSITIVE tool requiring approval — see
    # require_approval below). Not idempotentHint: a second call once the
    # resource is already revoked fails with "does not have access" rather
    # than succeeding as a no-op.
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
async def revoke_access(
    username: str, resource: str, approval_id: int, ticket_id: int | None = None
) -> dict:
    """Revoke an employee's access to a resource. SENSITIVE: requires a prior
    human-approved `approval_id` matching this exact tool call, or the server
    refuses the action.

    Checks the approval against "access_revoke_access" (this tool's namespaced
    gateway name) — see identity_server.py's disable_user for why this must
    match the namespaced name the agent actually planned with, not the bare
    tool name.
    """
    async with session_scope() as session:
        await require_approval(
            session, approval_id, "access_revoke_access", {"username": username, "resource": resource}
        )
        return await t.revoke_access(
            session, username, resource, actor="mcp-client", ticket_id=ticket_id
        )
