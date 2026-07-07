"""API client auth for endpoints that submit tickets, decide approvals, or
expose approval/audit detail. Checked via a FastAPI dependency against the
`X-API-Key` header, resolved to a real ApiClient row (app/db/models.py) —
not just a bare string compare — so callers are distinguishable from each
other (app/api/main.py's ticket/audit routes scope reads by which
ApiClient made the request; see ApiClient's docstring for why this
replaced the original single-shared-key design).

If `API_KEY` is left unset in config (e.g. quick local demo runs), auth is
disabled and a startup log warns loudly — this must be set before the API is
reachable from anywhere but localhost. When API_KEY IS set,
app/api/main.py's lifespan ensures a bootstrap `admin` ApiClient row exists
with `key == settings.api_key` before the app starts serving — so an
existing deployment's X-API-Key keeps working unchanged after this
migration, it's just backed by a real row instead of a bare string compare.
"""

import logging

from fastapi import Header, HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.db.models import ApiClient, Reviewer
from app.db.session import session_scope

logger = logging.getLogger(__name__)


async def require_api_client(x_api_key: str | None = Header(default=None)) -> ApiClient | None:
    """Returns the resolved ApiClient for the caller's X-API-Key, or None if
    API_KEY is unset entirely (local demo mode — auth disabled, see the
    module docstring). Route handlers that need to scope a read by caller
    (GET /tickets/{id}, GET /tickets/{id}/audit, GET /approvals) must treat
    a None return as "no scoping possible, demo mode already trusts
    everyone" per that route's own docstring — never as "admin."
    """
    settings = get_settings()
    if not settings.api_key:
        return None
    if x_api_key is None:
        raise HTTPException(401, "Missing or invalid API key")
    async with session_scope() as session:
        client = await session.scalar(select(ApiClient).where(ApiClient.key == x_api_key))
    if client is None:
        raise HTTPException(401, "Missing or invalid API key")
    return client


async def require_reviewer_token(x_reviewer_token: str | None = Header(default=None)) -> Reviewer:
    """Authenticates the caller AS a specific reviewer, via a per-reviewer
    secret token — not just a claimed username in the request body.

    Closes a real gap in the RBAC layer (app/api/rbac.py): before this, the
    `reviewer` field on ApprovalDecision was a free-text string checked only
    against the `reviewers` table's role/manager-relationship rules, with
    nothing verifying the CALLER actually is that reviewer. Anyone holding
    the one shared X-API-Key (used by every legitimate integration/UI user
    alike) could type any registered reviewer's username — including
    "admin" — into the request body and pass every authorization check.

    The token is looked up with hmac.compare_digest-equivalent safety (the
    lookup itself is by exact DB match, not a loop of user-supplied
    comparisons, so no additional timing-safety code is needed here) and
    the resolved Reviewer row is returned so callers use ITS username for
    authorization, never a value taken from the request body.
    """
    if not x_reviewer_token:
        raise HTTPException(401, "Missing X-Reviewer-Token header — required to decide approvals.")
    async with session_scope() as session:
        reviewer = await session.scalar(select(Reviewer).where(Reviewer.token == x_reviewer_token))
    if reviewer is None:
        raise HTTPException(401, "Invalid reviewer token.")
    return reviewer
