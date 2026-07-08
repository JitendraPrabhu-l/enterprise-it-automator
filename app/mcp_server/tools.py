"""Core tool implementations, independent of the MCP transport layer.

Kept separate from server.py so both the MCP server and tests can call these
directly without spinning up a JSON-RPC transport.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AuditLog, EmployeeUser, Ticket, UserStatus


class ToolError(Exception):
    """Raised for expected, user-facing tool failures (not found, bad state, etc.)."""


DEPARTMENT_ACCESS_DEFAULTS: dict[str, list[str]] = {
    "engineering": ["vpn", "github:engineering", "jira:core-platform"],
    "sales": ["vpn", "salesforce"],
    "it": ["vpn", "github:engineering", "admin-panel"],
    "hr": ["vpn", "workday"],
    "finance": ["vpn", "netsuite"],
    "executive": ["vpn", "admin-panel", "netsuite", "workday"],
}

# Domain prefixes the gateway (server.py) namespaces every tool name with —
# is_sensitive() strips these before checking against SENSITIVE_ACTIONS so
# the env var stays configured with bare action names regardless of
# namespacing, and approval_gate checks work against whichever form a
# caller uses.
_DOMAIN_PREFIXES = ("identity_", "access_", "ticketing_")


def strip_domain_prefix(tool_name: str) -> str:
    """Strips a gateway domain prefix (identity_/access_/ticketing_) from a
    namespaced tool name, if present — the bare name is what
    SENSITIVE_ACTIONS and TOOLS_ACCEPTING_TICKET_ID are configured/defined
    against, so callers holding either form (namespaced, from the LLM's
    plan, or bare, e.g. in a test) get the same answer.
    """
    for prefix in _DOMAIN_PREFIXES:
        if tool_name.startswith(prefix):
            return tool_name[len(prefix):]
    return tool_name


# Tools whose MCP function signature accepts a ticket_id param — either
# optional (create_user/disable_user/grant_access/revoke_access, for
# audit-log attribution) or REQUIRED (add_ticket_comment/get_ticket_status,
# app/mcp_server/ticketing_server.py — the whole point of those two tools
# is acting on a specific ticket). Read-only/meta tools that don't accept
# it at all (get_user, is_sensitive_action) are deliberately excluded, and
# FastMCP rejects an unexpected kwarg, so this must be an allowlist, not
# "inject into every call."
#
# add_ticket_comment/get_ticket_status were added to this set after a live
# deployment bug: discover_tool_reference() (app/agent/graph.py) surfaces
# every gateway tool to the planner, including these two — nothing stopped
# the LLM from planning ticketing_add_ticket_comment even though no
# category prompt ever tells it to, and when it did, the call failed
# FastMCP's own arg validation every time (ticket_id is required, not
# optional, and nothing supplied it). Previously only the four identity/
# access tools were on this allowlist; kept in sync with the domain
# servers' actual signatures by hand, same as SENSITIVE_ACTIONS being
# hand-configured rather than introspected.
TOOLS_ACCEPTING_TICKET_ID = {
    "create_user", "disable_user", "grant_access", "revoke_access",
    "add_ticket_comment", "get_ticket_status",
}


def accepts_ticket_id(tool_name: str) -> bool:
    return strip_domain_prefix(tool_name) in TOOLS_ACCEPTING_TICKET_ID


def default_access_for_department(department: str) -> list[str]:
    return list(DEPARTMENT_ACCESS_DEFAULTS.get(department.strip().lower(), ["vpn"]))


async def _get_user(session: AsyncSession, username: str) -> EmployeeUser:
    user = await session.scalar(
        select(EmployeeUser).where(EmployeeUser.username == username)
    )
    if user is None:
        raise ToolError(f"No such user: {username!r}")
    return user


async def _get_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        raise ToolError(f"No such ticket: {ticket_id}")
    return ticket


async def _audit(
    session: AsyncSession,
    actor: str,
    tool_name: str,
    tool_args: dict,
    result: str,
    success: bool,
    ticket_id: int | None = None,
) -> None:
    session.add(
        AuditLog(
            ticket_id=ticket_id,
            actor=actor,
            tool_name=tool_name,
            tool_args=tool_args,
            result=result,
            success=success,
        )
    )


def is_sensitive(tool_name: str) -> bool:
    return strip_domain_prefix(tool_name) in get_settings().sensitive_action_set


async def get_user(session: AsyncSession, username: str) -> dict:
    user = await _get_user(session, username)
    return {
        "username": user.username,
        "full_name": user.full_name,
        "email": user.email,
        "department": user.department,
        "status": user.status.value,
        "access_grants": user.access_grants,
    }


async def create_user(
    session: AsyncSession,
    username: str,
    full_name: str,
    email: str,
    department: str = "",
    actor: str = "agent",
    ticket_id: int | None = None,
) -> dict:
    existing = await session.scalar(
        select(EmployeeUser).where(EmployeeUser.username == username)
    )
    if existing is not None:
        raise ToolError(f"User already exists: {username!r}")

    # EmployeeUser.owned_by_client_id mirrors whoever owns the ticket that
    # triggered this creation (Ticket.submitted_by_client_id) — NULL for a
    # ticket with no attributable client (ADMIN/API_KEY-unset), or one
    # submitted directly by an ADMIN. Set here rather than passed in as a
    # tool arg: the LLM planner never sees or controls this value, exactly
    # like ticket_id itself (see graph.py's _EXECUTOR_INJECTED_ARGS).
    owned_by_client_id = None
    if ticket_id is not None:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is not None:
            owned_by_client_id = ticket.submitted_by_client_id

    default_access = default_access_for_department(department)
    user = EmployeeUser(
        username=username,
        full_name=full_name,
        email=email,
        department=department,
        status=UserStatus.ACTIVE,
        access_grants=default_access,
        owned_by_client_id=owned_by_client_id,
    )
    session.add(user)
    await session.flush()
    await _audit(
        session, actor, "create_user",
        {"username": username, "full_name": full_name, "email": email, "department": department},
        f"created user {username} with default {department!r} access: {', '.join(default_access)}",
        True, ticket_id,
    )
    return {"username": user.username, "status": user.status.value, "access_grants": user.access_grants}


async def disable_user(
    session: AsyncSession,
    username: str,
    actor: str = "agent",
    ticket_id: int | None = None,
) -> dict:
    """Sensitive action — must only be invoked after HITL approval."""
    user = await _get_user(session, username)
    if user.status == UserStatus.DISABLED:
        raise ToolError(f"User {username!r} is already disabled")
    user.status = UserStatus.DISABLED
    await _audit(
        session, actor, "disable_user", {"username": username},
        f"disabled user {username}", True, ticket_id,
    )
    return {"username": user.username, "status": user.status.value}


async def grant_access(
    session: AsyncSession,
    username: str,
    resource: str,
    actor: str = "agent",
    ticket_id: int | None = None,
) -> dict:
    user = await _get_user(session, username)
    if resource not in user.access_grants:
        user.access_grants = [*user.access_grants, resource]
    await _audit(
        session, actor, "grant_access", {"username": username, "resource": resource},
        f"granted {resource} to {username}", True, ticket_id,
    )
    return {"username": user.username, "access_grants": user.access_grants}


async def revoke_access(
    session: AsyncSession,
    username: str,
    resource: str,
    actor: str = "agent",
    ticket_id: int | None = None,
) -> dict:
    """Sensitive action — must only be invoked after HITL approval."""
    user = await _get_user(session, username)
    if resource not in user.access_grants:
        raise ToolError(f"User {username!r} does not have access to {resource!r}")
    user.access_grants = [g for g in user.access_grants if g != resource]
    await _audit(
        session, actor, "revoke_access", {"username": username, "resource": resource},
        f"revoked {resource} from {username}", True, ticket_id,
    )
    return {"username": user.username, "access_grants": user.access_grants}


async def add_ticket_comment(session: AsyncSession, ticket_id: int, comment: str) -> dict:
    """Simulated ticketing-system sync: appends to the ticket's own
    result_summary field, standing in for a real Jira/ServiceNow API call
    that would post a comment back to the external system of record.
    """
    ticket = await _get_ticket(session, ticket_id)
    ticket.result_summary = f"{ticket.result_summary}\n{comment}".strip()
    await _audit(
        session, "mcp-client", "ticketing_add_ticket_comment", {"ticket_id": ticket_id, "comment": comment},
        f"posted comment to ticket {ticket_id}", True, ticket_id,
    )
    return {"ticket_id": ticket_id, "comment": comment}


async def get_ticket_status(session: AsyncSession, ticket_id: int) -> dict:
    ticket = await _get_ticket(session, ticket_id)
    return {"ticket_id": ticket.id, "status": ticket.status.value, "result_summary": ticket.result_summary}
