"""Core tool implementations, independent of the MCP transport layer.

Kept separate from server.py so both the MCP server and tests can call these
directly without spinning up a JSON-RPC transport.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.audit import append_audit_log
from app.db.models import EmployeeUser, Ticket, UserStatus


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
    "create_user", "disable_user", "enable_user", "grant_access", "revoke_access",
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


async def _get_user_or_audit_rejection(
    session: AsyncSession, username: str, *, actor: str, tool_name: str,
    tool_args: dict, ticket_id: int | None,
) -> EmployeeUser:
    """Same lookup as _get_user, but for a caller that's about to attempt a
    mutating/sensitive action: a "no such user" outcome is audited as a
    rejected attempt (success=False) before the ToolError propagates, same
    as the already-disabled/doesn't-have-access rejections below — every
    ATTEMPT at one of these actions leaves a record, not just successful
    ones. Not used by get_user (read-only, never audited at all, before or
    after this change).
    """
    user = await session.scalar(select(EmployeeUser).where(EmployeeUser.username == username))
    if user is None:
        await _audit(
            session, actor, tool_name, tool_args,
            f"rejected: no such user: {username}", False, ticket_id,
            commit_immediately=True,
        )
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
    *,
    commit_immediately: bool = False,
) -> None:
    """commit_immediately=True is required for every rejection-path call
    (a _audit(..., success=False, ...) immediately followed by `raise
    ToolError(...)`): every tool here is invoked as
    `async with session_scope() as session: ...` at the MCP-server layer
    (see identity_server.py/access_server.py), and session_scope() rolls
    back the ENTIRE session on any exception leaving that block — including
    a session.add() that ran moments before the raise. Without an explicit
    commit here, the audit row for a rejected attempt would be silently
    discarded by that rollback, defeating the entire point of auditing
    rejections at all. Confirmed live via a direct reproduction: a
    rejected create_user's audit row was gone after the ToolError
    propagated, until this flag was added. Success-path calls don't need
    this — they're followed by a normal return, so session_scope()'s own
    commit-on-clean-exit covers them already.
    """
    await append_audit_log(
        session,
        ticket_id=ticket_id,
        actor=actor,
        tool_name=tool_name,
        tool_args=tool_args,
        result=result,
        success=success,
    )
    if commit_immediately:
        await session.commit()


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
        # Audited even though rejected: previously a no-op attempt like this
        # left ZERO audit trail (ToolError raised before _audit() ran),
        # indistinguishable from the tool never having been called at all —
        # found live via a ticket whose audit log showed only its follow-up
        # comment, with no record the agent had first tried and been
        # refused. Every ATTEMPT at a sensitive/mutating action is now
        # audited, success or not — see disable_user/revoke_access below.
        await _audit(
            session, actor, "create_user", {"username": username, "full_name": full_name, "email": email},
            f"rejected: user already exists: {username}", False, ticket_id,
            commit_immediately=True,
        )
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
    user = await _get_user_or_audit_rejection(
        session, username, actor=actor, tool_name="disable_user",
        tool_args={"username": username}, ticket_id=ticket_id,
    )
    if user.status == UserStatus.DISABLED:
        await _audit(
            session, actor, "disable_user", {"username": username},
            f"rejected: user already disabled: {username}", False, ticket_id,
            commit_immediately=True,
        )
        raise ToolError(f"User {username!r} is already disabled")
    user.status = UserStatus.DISABLED
    await _audit(
        session, actor, "disable_user", {"username": username},
        f"disabled user {username}", True, ticket_id,
    )
    return {"username": user.username, "status": user.status.value}


async def enable_user(
    session: AsyncSession,
    username: str,
    actor: str = "agent",
    ticket_id: int | None = None,
) -> dict:
    """Sensitive action — must only be invoked after HITL approval.

    Re-activates a previously disabled (offboarded) employee — the only
    real re-onboarding path this system supports. Added after a live bug:
    an onboarding ticket for an employee who already exists but is disabled
    had no real tool to reach for, and the LLM planner hallucinated a
    nonexistent identity_enable_user call instead of failing cleanly. This
    IS that tool now, and it's deliberately sensitive (same bar as
    disable_user) — reactivating a departed employee's account/access is
    just as security-relevant as disabling one, not a routine onboarding
    step.
    """
    user = await _get_user_or_audit_rejection(
        session, username, actor=actor, tool_name="enable_user",
        tool_args={"username": username}, ticket_id=ticket_id,
    )
    if user.status == UserStatus.ACTIVE:
        await _audit(
            session, actor, "enable_user", {"username": username},
            f"rejected: user already active: {username}", False, ticket_id,
            commit_immediately=True,
        )
        raise ToolError(f"User {username!r} is already active")
    user.status = UserStatus.ACTIVE
    await _audit(
        session, actor, "enable_user", {"username": username},
        f"re-enabled user {username}", True, ticket_id,
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
    user = await _get_user_or_audit_rejection(
        session, username, actor=actor, tool_name="revoke_access",
        tool_args={"username": username, "resource": resource}, ticket_id=ticket_id,
    )
    if resource not in user.access_grants:
        await _audit(
            session, actor, "revoke_access", {"username": username, "resource": resource},
            f"rejected: user does not have access to {resource}: {username}", False, ticket_id,
            commit_immediately=True,
        )
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
