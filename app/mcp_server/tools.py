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
    bare_name = tool_name
    for prefix in _DOMAIN_PREFIXES:
        if tool_name.startswith(prefix):
            bare_name = tool_name[len(prefix):]
            break
    return bare_name in get_settings().sensitive_action_set


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

    default_access = default_access_for_department(department)
    user = EmployeeUser(
        username=username,
        full_name=full_name,
        email=email,
        department=department,
        status=UserStatus.ACTIVE,
        access_grants=default_access,
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
