"""Tests for lightweight role/relationship-based approval authorization
(Stage 4.2, scoped down — see app/api/rbac.py's module docstring)."""

import pytest

from app.api.rbac import ApprovalNotAuthorizedError, authorize_reviewer, find_entitled_reviewers
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


# --- find_entitled_reviewers: used by app/notifications/telegram.py to
# decide who to notify — must return exactly the reviewers authorize_reviewer
# would let through, since it's the inverse of the same rule. -------------


async def test_find_entitled_reviewers_includes_every_it_admin(session):
    session.add(Reviewer(username="admin1", role=ReviewerRole.IT_ADMIN))
    session.add(Reviewer(username="admin2", role=ReviewerRole.IT_ADMIN))
    session.add(Reviewer(username="mchen", role=ReviewerRole.MANAGER))
    await _make_employee(session, username="jsmith", manager_username="someone_else")
    approval = await _make_approval(session, tool_args={"username": "jsmith"})

    entitled = await find_entitled_reviewers(session, approval)
    usernames = {r.username for r in entitled}
    assert usernames == {"admin1", "admin2"}


async def test_find_entitled_reviewers_includes_the_direct_manager(session):
    session.add(Reviewer(username="mchen", role=ReviewerRole.MANAGER))
    session.add(Reviewer(username="other_manager", role=ReviewerRole.MANAGER))
    await _make_employee(session, username="jsmith", manager_username="mchen")
    approval = await _make_approval(session, tool_args={"username": "jsmith"})

    entitled = await find_entitled_reviewers(session, approval)
    usernames = {r.username for r in entitled}
    assert usernames == {"mchen"}


async def test_find_entitled_reviewers_empty_when_no_target_and_no_admins(session):
    session.add(Reviewer(username="mchen", role=ReviewerRole.MANAGER))
    approval = await _make_approval(session, tool_name="some_tool", tool_args={})

    entitled = await find_entitled_reviewers(session, approval)
    assert entitled == []


async def test_find_entitled_reviewers_matches_authorize_reviewer_exactly(session):
    """Every reviewer find_entitled_reviewers returns must actually pass
    authorize_reviewer for the same approval, and vice versa — they're
    supposed to be exact inverses of the same rule."""
    session.add(Reviewer(username="admin", role=ReviewerRole.IT_ADMIN))
    session.add(Reviewer(username="mchen", role=ReviewerRole.MANAGER))
    session.add(Reviewer(username="other_manager", role=ReviewerRole.MANAGER))
    await _make_employee(session, username="jsmith", manager_username="mchen")
    approval = await _make_approval(session, tool_args={"username": "jsmith"})

    entitled_usernames = {r.username for r in await find_entitled_reviewers(session, approval)}

    for username in ("admin", "mchen", "other_manager"):
        should_be_entitled = username in entitled_usernames
        try:
            await authorize_reviewer(session, username, approval)
            actually_entitled = True
        except ApprovalNotAuthorizedError:
            actually_entitled = False
        assert should_be_entitled == actually_entitled, username
