import asyncio
import secrets

from sqlalchemy import select

from app.db.models import ApiClient, ApiClientRole, EmployeeUser, Reviewer, ReviewerRole, UserStatus
from app.db.session import init_db, session_scope

SEED_USERS = [
    dict(
        username="jsmith",
        full_name="Jane Smith",
        email="jsmith@example.com",
        department="Engineering",
        status=UserStatus.ACTIVE,
        access_grants=["github:engineering", "jira:core-platform", "vpn"],
        manager_username="mchen",
    ),
    dict(
        username="rjones",
        full_name="Raj Jones",
        email="rjones@example.com",
        department="Sales",
        status=UserStatus.ACTIVE,
        access_grants=["salesforce", "vpn"],
        manager_username="mchen",
    ),
]

# Reviewers who may decide sensitive-action approvals (Stage 4.2, scoped
# down — see app/api/rbac.py). mchen is jsmith/rjones's manager above, so
# can decide approvals targeting either of them; admin is an it_admin and
# can decide any approval regardless of who it targets. Each gets a random
# per-reviewer token generated at seed time (see app/api/auth.py's
# require_reviewer_token) — this is what actually proves a caller IS that
# reviewer, rather than the username alone, which is just a claim anyone
# holding the shared API key could type into a request.
SEED_REVIEWER_USERNAMES = [
    dict(username="mchen", role=ReviewerRole.MANAGER),
    dict(username="admin", role=ReviewerRole.IT_ADMIN),
]

# Example STANDARD ApiClient demonstrating the caller-scoped reads added
# after a security review (see ApiClient's docstring in app/db/models.py):
# a client with this key can submit tickets and see only tickets whose
# `requester` matches its own `name` — not every employee's ticket/audit
# history the way an ADMIN client (or the bootstrap admin client
# app/api/main.py's lifespan creates from API_KEY) can. Not seeded with a
# fixed key — a fresh random one is generated and printed, same as
# reviewer tokens below.
SEED_STANDARD_API_CLIENTS = [
    dict(name="hr@example.com", role=ApiClientRole.STANDARD, daily_request_limit=100),
]


async def seed() -> None:
    await init_db()
    issued_tokens: dict[str, str] = {}
    issued_api_keys: dict[str, str] = {}
    async with session_scope() as session:
        for row in SEED_USERS:
            existing = await session.scalar(
                select(EmployeeUser).where(EmployeeUser.username == row["username"])
            )
            if existing is None:
                session.add(EmployeeUser(**row))
        for row in SEED_REVIEWER_USERNAMES:
            existing = await session.scalar(
                select(Reviewer).where(Reviewer.username == row["username"])
            )
            if existing is None:
                token = secrets.token_urlsafe(24)
                session.add(Reviewer(token=token, **row))
                issued_tokens[row["username"]] = token
        for row in SEED_STANDARD_API_CLIENTS:
            existing = await session.scalar(
                select(ApiClient).where(ApiClient.name == row["name"])
            )
            if existing is None:
                key = secrets.token_urlsafe(32)
                session.add(ApiClient(key=key, **row))
                issued_api_keys[row["name"]] = key
    print(
        f"Seeded {len(SEED_USERS)} mock employee users, {len(SEED_REVIEWER_USERNAMES)} reviewers, "
        f"and {len(SEED_STANDARD_API_CLIENTS)} example standard API client."
    )
    if issued_tokens:
        print("\nReviewer tokens (save these — shown only once, at creation time):")
        for username, token in issued_tokens.items():
            print(f"  {username}: {token}")
        print(
            "\nPass one as the X-Reviewer-Token header when calling "
            "POST /approvals/{id}/decide, e.g.:\n"
            '  curl -X POST http://127.0.0.1:8000/approvals/1/decide \\\n'
            '    -H "X-API-Key: $API_KEY" -H "X-Reviewer-Token: <token above>" \\\n'
            "    -H \"Content-Type: application/json\" -d '{\"approve\": true}'"
        )
    if issued_api_keys:
        print("\nStandard API client keys (save these — shown only once, at creation time):")
        for name, key in issued_api_keys.items():
            print(f"  {name}: {key}")
        print(
            "\nA request using this key can only see tickets it filed itself "
            "(requester must match the client's name above) — unlike the admin "
            "API_KEY, which sees everything."
        )


if __name__ == "__main__":
    asyncio.run(seed())
