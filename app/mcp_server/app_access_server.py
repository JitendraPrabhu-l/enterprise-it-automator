"""App access domain: granting and revoking NAMED SaaS application access
(Jira, Slack, Salesforce, GitHub, Workday, NetSuite, email — see
app/mcp_server/tools.py's APP_CATALOG) for employees.

Distinct from access_server.py's generic grant_access/revoke_access, which
takes an arbitrary free-text resource string with no per-item metadata —
this domain models a fixed catalog of real named apps as first-class
grants with their own state/timestamps/audit trail (app/db/models.py's
AppAccessGrant), so "does this person currently have Slack" is a real
query, not a string-match against a flat list. The two domains coexist
deliberately (see AppAccessGrant's docstring) — this is additive, not a
replacement for the generic tools.

Standalone FastMCP instance so this domain's tools could run as their own
process, mirroring identity_server.py/access_server.py/ticketing_server.py
— see server.py, which composes all four under one gateway process for
local/dev use via add_tool() namespacing.
"""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.db.session import session_scope
from app.mcp_server import tools as t
from app.mcp_server.approval_gate import require_approval

app_access_mcp = FastMCP("app_access")


@app_access_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def list_app_access(username: str) -> dict:
    """List every named SaaS app (jira, slack, salesforce, github, workday,
    netsuite, email) this employee currently has active access to."""
    async with session_scope() as session:
        return await t.list_app_access(session, username)


@app_access_mcp.tool(
    # idempotentHint=True: a repeat grant call while access is already
    # active is a genuine no-op (tools.py's grant_app_access reuses the
    # existing ACTIVE row rather than duplicating it), same rationale as
    # access_server.py's grant_access.
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def grant_app_access(
    username: str, app_name: str, ticket_id: int | None = None
) -> dict:
    """Grant an employee access to a named SaaS app (jira, slack, salesforce,
    github, workday, netsuite, or email). Not a destructive action — but IS
    in the SENSITIVE_ACTIONS set by default (same as generic grant_access),
    so the graph still pauses for human approval before this tool is ever
    called; unlike revoke_app_access, this tool itself doesn't additionally
    call require_approval server-side (matching generic grant_access's own
    precedent — access_server.py), so the HITL gate here is the graph-level
    one, not a second MCP-transport-level check."""
    async with session_scope() as session:
        return await t.grant_app_access(
            session, username, app_name, actor="mcp-client", ticket_id=ticket_id
        )


@app_access_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
async def revoke_app_access(
    username: str, app_name: str, approval_id: int, ticket_id: int | None = None
) -> dict:
    """Revoke an employee's access to a named SaaS app. SENSITIVE: requires
    a prior human-approved `approval_id` matching this exact tool call, or
    the server refuses the action.

    Checks the approval against "app_access_revoke_app_access" (this
    tool's namespaced gateway name) — see identity_server.py's
    disable_user for why this must match the namespaced name the agent
    actually planned with, not the bare tool name.
    """
    async with session_scope() as session:
        await require_approval(
            session, approval_id, "app_access_revoke_app_access",
            {"username": username, "app_name": app_name},
        )
        return await t.revoke_app_access(
            session, username, app_name, actor="mcp-client", ticket_id=ticket_id
        )
