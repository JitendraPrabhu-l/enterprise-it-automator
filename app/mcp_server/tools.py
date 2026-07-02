"""Core tool implementations, independent of the MCP transport layer.

Kept separate from server.py so both the MCP server and tests can call these
directly without spinning up a JSON-RPC transport.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AuditLog, EmployeeUser, UserStatus


class ToolError(Exception):
    """Raised for expected, user-facing tool failures (not found, bad state, etc.)."""


async def _get_user(session: AsyncSession, username: str) -> EmployeeUser:
    user = await session.scalar(
        select(EmployeeUser).where(EmployeeUser.username == username)
    )
    if user is None:
        raise ToolError(f"No such user: {username!r}")
    return user


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
    return tool_name in get_settings().sensitive_action_set


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

    user = EmployeeUser(
        username=username,
        full_name=full_name,
        email=email,
        department=department,
        status=UserStatus.ACTIVE,
        access_grants=[],
    )
    session.add(user)
    await session.flush()
    await _audit(
        session, actor, "create_user",
        {"username": username, "full_name": full_name, "email": email, "department": department},
        f"created user {username}", True, ticket_id,
    )
    return {"username": user.username, "status": user.status.value}


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
