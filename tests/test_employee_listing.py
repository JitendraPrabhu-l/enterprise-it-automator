from sqlalchemy import select

from app.db.models import EmployeeUser, UserStatus


async def _add_employee(session, **overrides):
    defaults = dict(
        username="jsmith", full_name="Jane Smith", email="j@example.com",
        department="Engineering", status=UserStatus.ACTIVE, access_grants=[],
    )
    defaults.update(overrides)
    employee = EmployeeUser(**defaults)
    session.add(employee)
    await session.flush()
    return employee


async def test_query_active_employees(session):
    await _add_employee(session, username="jsmith", status=UserStatus.ACTIVE)
    await _add_employee(session, username="old_bob", status=UserStatus.DISABLED)

    rows = list(await session.scalars(
        select(EmployeeUser).where(EmployeeUser.status == UserStatus.ACTIVE)
    ))
    assert [r.username for r in rows] == ["jsmith"]


async def test_query_disabled_employees(session):
    await _add_employee(session, username="jsmith", status=UserStatus.ACTIVE)
    await _add_employee(session, username="old_bob", status=UserStatus.DISABLED)

    rows = list(await session.scalars(
        select(EmployeeUser).where(EmployeeUser.status == UserStatus.DISABLED)
    ))
    assert [r.username for r in rows] == ["old_bob"]


async def test_query_all_employees_unfiltered(session):
    await _add_employee(session, username="jsmith", status=UserStatus.ACTIVE)
    await _add_employee(session, username="old_bob", status=UserStatus.DISABLED)

    rows = list(await session.scalars(select(EmployeeUser)))
    assert {r.username for r in rows} == {"jsmith", "old_bob"}
