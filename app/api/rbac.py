"""Lightweight role/relationship-based approval authorization (Stage 4.2,
scoped down — see ROADMAP.md's Stage 4 trap notes: a real OIDC-verified
identity provider was explicitly out of scope for this project; instead,
app/api/auth.py's require_reviewer_token authenticates the caller as a
specific Reviewer via a per-reviewer secret token, and this module decides
what that authenticated reviewer is entitled to do).

Rule: an `it_admin` reviewer may decide any sensitive approval. A `manager`
reviewer may only decide approvals whose target employee's
`manager_username` matches them. The `reviewer_username` passed in here
must already be the AUTHENTICATED caller's username (resolved from their
token, never from a request body) — this function only handles
authorization (what are they allowed to do), not authentication (are they
who they claim to be).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, EmployeeUser, Reviewer, ReviewerRole


class ApprovalNotAuthorizedError(Exception):
    """Raised when a reviewer is unknown, or known but not entitled to
    decide this specific approval."""


def _target_username(approval: Approval) -> str | None:
    """The employee this approval's sensitive action targets, if any — every
    sensitive tool in this codebase (disable_user, revoke_access) takes a
    `username` argument, so this covers the real cases today without
    hardcoding a specific tool name here.
    """
    return approval.tool_args.get("username")


async def authorize_reviewer(session: AsyncSession, reviewer_username: str, approval: Approval) -> None:
    """Raises ApprovalNotAuthorizedError if reviewer_username may not decide
    this approval. Call before mutating Approval.status.
    """
    reviewer = await session.scalar(select(Reviewer).where(Reviewer.username == reviewer_username))
    if reviewer is None:
        raise ApprovalNotAuthorizedError(
            f"{reviewer_username!r} is not a registered reviewer — cannot decide approvals."
        )

    if reviewer.role == ReviewerRole.IT_ADMIN:
        return

    target_username = _target_username(approval)
    if not target_username:
        raise ApprovalNotAuthorizedError(
            f"{reviewer_username!r} (role={reviewer.role.value}) is not entitled to decide "
            f"approval {approval.id} — its target action has no identifiable employee, "
            f"so only an it_admin reviewer may decide it."
        )

    employee = await session.scalar(select(EmployeeUser).where(EmployeeUser.username == target_username))
    if employee is None or employee.manager_username != reviewer_username:
        raise ApprovalNotAuthorizedError(
            f"{reviewer_username!r} (role={reviewer.role.value}) is not the manager of "
            f"{target_username!r} — not entitled to decide approval {approval.id}."
        )


async def find_entitled_reviewers(session: AsyncSession, approval: Approval) -> list[Reviewer]:
    """The inverse of authorize_reviewer: every Reviewer who WOULD be let
    through authorize_reviewer for this approval, used by
    app/notifications/telegram.py to decide who to notify. Every it_admin
    always qualifies (mirrors authorize_reviewer's own unconditional
    it_admin bypass); a manager qualifies only if the approval's target
    employee's manager_username matches them, same rule, just inverted from
    "can THIS reviewer decide THIS approval" to "which reviewers can decide
    THIS approval."
    """
    reviewers = list(await session.scalars(select(Reviewer)))
    target_username = _target_username(approval)
    manager_username = None
    if target_username:
        employee = await session.scalar(select(EmployeeUser).where(EmployeeUser.username == target_username))
        if employee is not None:
            manager_username = employee.manager_username

    return [
        reviewer
        for reviewer in reviewers
        if reviewer.role == ReviewerRole.IT_ADMIN or reviewer.username == manager_username
    ]
