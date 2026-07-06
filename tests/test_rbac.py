"""Tests for lightweight role/relationship-based approval authorization
(Stage 4.2, scoped down — see app/api/rbac.py's module docstring)."""

import pytest

from app.api.rbac import ApprovalNotAuthorizedError, authorize_reviewer
from app.db.models import Approval, ApprovalStatus, EmployeeUser, Reviewer, ReviewerRole, UserStatus


async def _make_employee(session, username="jsmith", manager_username="mchen"):
    employee = EmployeeUser(
        username=username,
        full_name="Test Employee",
        email=f"{username}@example.com",
        department="Engineering",
        status=UserStatus.ACTIVE,
        manager_username=manager_username,
    )
    session.add(employee)
    await session.flush()
    return employee


async def _make_approval(session, tool_name="disable_user", tool_args=None, ticket_id=1):
    approval = Approval(
        ticket_id=ticket_id,
        tool_name=tool_name,
        tool_args={"username": "jsmith"} if tool_args is None else tool_args,
        status=ApprovalStatus.PENDING,
    )
    session.add(approval)
    await session.flush()
    return approval


async def test_it_admin_may_decide_any_approval(session):
    session.add(Reviewer(username="admin", role=ReviewerRole.IT_ADMIN))
    await _make_employee(session, username="jsmith", manager_username="someone_else")
    approval = await _make_approval(session, tool_args={"username": "jsmith"})

    await authorize_reviewer(session, "admin", approval)  # must not raise


async def test_manager_may_decide_approval_for_their_direct_report(session):
    session.add(Reviewer(username="mchen", role=ReviewerRole.MANAGER))
    await _make_employee(session, username="jsmith", manager_username="mchen")
    approval = await _make_approval(session, tool_args={"username": "jsmith"})

    await authorize_reviewer(session, "mchen", approval)  # must not raise


async def test_manager_may_not_decide_approval_for_non_report(session):
    session.add(Reviewer(username="mchen", role=ReviewerRole.MANAGER))
    await _make_employee(session, username="jsmith", manager_username="someone_else")
    approval = await _make_approval(session, tool_args={"username": "jsmith"})

    with pytest.raises(ApprovalNotAuthorizedError, match="not the manager of"):
        await authorize_reviewer(session, "mchen", approval)


async def test_unregistered_reviewer_is_rejected(session):
    await _make_employee(session, username="jsmith", manager_username="mchen")
    approval = await _make_approval(session, tool_args={"username": "jsmith"})

    with pytest.raises(ApprovalNotAuthorizedError, match="not a registered reviewer"):
        await authorize_reviewer(session, "rando", approval)


async def test_manager_rejected_when_target_employee_unknown(session):
    session.add(Reviewer(username="mchen", role=ReviewerRole.MANAGER))
    approval = await _make_approval(session, tool_args={"username": "ghost"})

    with pytest.raises(ApprovalNotAuthorizedError, match="not the manager of"):
        await authorize_reviewer(session, "mchen", approval)


async def test_manager_rejected_when_approval_has_no_target_username(session):
    session.add(Reviewer(username="mchen", role=ReviewerRole.MANAGER))
    approval = await _make_approval(session, tool_name="some_tool", tool_args={})

    with pytest.raises(ApprovalNotAuthorizedError, match="no identifiable employee"):
        await authorize_reviewer(session, "mchen", approval)
