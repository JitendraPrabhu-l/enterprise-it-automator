import asyncio

from sqlalchemy import select

from app.db.models import EmployeeUser, UserStatus
from app.db.session import init_db, session_scope

SEED_USERS = [
    dict(
        username="jsmith",
        full_name="Jane Smith",
        email="jsmith@example.com",
        department="Engineering",
        status=UserStatus.ACTIVE,
        access_grants=["github:engineering", "jira:core-platform", "vpn"],
    ),
    dict(
        username="rjones",
        full_name="Raj Jones",
        email="rjones@example.com",
        department="Sales",
        status=UserStatus.ACTIVE,
        access_grants=["salesforce", "vpn"],
    ),
]


async def seed() -> None:
    await init_db()
    async with session_scope() as session:
        for row in SEED_USERS:
            existing = await session.scalar(
                select(EmployeeUser).where(EmployeeUser.username == row["username"])
            )
            if existing is None:
                session.add(EmployeeUser(**row))
    print(f"Seeded {len(SEED_USERS)} mock employee users.")


if __name__ == "__main__":
    asyncio.run(seed())
