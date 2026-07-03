"""Server-side enforcement of human-in-the-loop approval for sensitive tools.

The agent cannot simply decide an action is "approved" client-side — the MCP
server itself refuses to execute a sensitive tool unless the caller presents
the id of an Approval row that a human has actually marked APPROVED in the
database. This is what makes HITL a real security boundary rather than a
prompt-level suggestion.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, ApprovalStatus, utcnow
from app.mcp_server.tools import ToolError


async def require_approval(
    session: AsyncSession, approval_id: int, tool_name: str, tool_args: dict
) -> Approval:
    """Validates approval_id authorizes exactly this tool_name/tool_args
    call, then marks it consumed (executed_at) so THIS SAME approval can
    never authorize a second execution — without this, one human sign-off
    would let the underlying sensitive action be invoked an unlimited
    number of times by anyone who knows or guesses the approval_id (e.g.
    over the streamable-HTTP MCP transport, which has no caller auth of its
    own). status stays APPROVED rather than moving to a new terminal state,
    since existing audit/display logic already keys off APPROVED and a
    second terminal state would be redundant with executed_at.
    """
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise ToolError(f"Unknown approval_id: {approval_id}")
    if approval.status != ApprovalStatus.APPROVED:
        raise ToolError(
            f"Approval {approval_id} is {approval.status.value}, not approved — "
            "sensitive action refused."
        )
    if approval.executed_at is not None:
        raise ToolError(
            f"Approval {approval_id} was already used to execute {approval.tool_name!r} "
            f"at {approval.executed_at.isoformat()} — refusing to reuse it for another execution."
        )
    if approval.tool_name != tool_name:
        raise ToolError(
            f"Approval {approval_id} was granted for tool {approval.tool_name!r}, "
            f"not {tool_name!r} — refusing to reuse it for a different action."
        )
    if approval.tool_args != tool_args:
        raise ToolError(
            f"Approval {approval_id} arguments do not match the requested call — "
            "refusing to reuse it for different arguments."
        )
    approval.executed_at = utcnow()
    return approval


async def find_approved(
    session: AsyncSession, ticket_id: int, tool_name: str, tool_args: dict
) -> Approval | None:
    """Convenience lookup used by the agent to find an already-approved gate."""
    result = await session.scalars(
        select(Approval).where(
            Approval.ticket_id == ticket_id,
            Approval.tool_name == tool_name,
            Approval.status == ApprovalStatus.APPROVED,
        )
    )
    for approval in result:
        if approval.tool_args == tool_args:
            return approval
    return None
