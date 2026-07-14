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
from dataclasses import dataclass

from fastapi import Header, HTTPException
from sqlalchemy import select

from app.api.oidc import OIDCVerificationError, verify_oidc_token
from app.api.security_audit import record_security_event
from app.config import get_settings
from app.db.models import ApiClient, Reviewer
from app.db.session import session_scope

logger = logging.getLogger(__name__)


@dataclass
class AuthenticatedReviewer:
    """A reviewer plus HOW they authenticated — the provenance half is what
    the approvals table records (reviewer_auth_method/reviewer_oidc_subject
    columns), so an auditor can distinguish 'the IdP vouched for this
    person' from 'someone presented this reviewer's shared secret.'
    """

    reviewer: Reviewer
    auth_method: str  # "token" | "oidc"
    oidc_subject: str | None = None


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
        await record_security_event(actor="auth", event="invalid_api_key")
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
        await record_security_event(actor="auth", event="invalid_reviewer_token")
        raise HTTPException(401, "Invalid reviewer token.")
    return reviewer


async def require_reviewer(
    x_reviewer_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> AuthenticatedReviewer:
    """Authenticates the caller as a reviewer via EITHER path:

    - `Authorization: Bearer <OIDC JWT>` — only honored when OIDC is
      configured (Settings.oidc_enabled); the token is fully verified
      against the IdP's published keys (app/api/oidc.py), then its username
      claim must match a registered Reviewer row. IdP-verified identity, no
      local secret involved.
    - `X-Reviewer-Token` — the original per-reviewer shared secret,
      unchanged. Still the only path when OIDC is not configured, and still
      accepted when it is (a migration period where some reviewers use SSO
      and some haven't onboarded yet is the normal enterprise rollout shape,
      and killing the old path is a one-line change — delete this fallback —
      once a deployment decides to).

    Precedence: a presented Bearer token is verified and is AUTHORITATIVE —
    if it fails verification the request is rejected even if a valid
    X-Reviewer-Token accompanies it. Silently falling back from a bad JWT
    to a weaker credential would let an attacker with a stolen reviewer
    token mask it behind an expired-JWT smokescreen in the logs.
    """
    settings = get_settings()
    if settings.oidc_enabled and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        try:
            claims = await verify_oidc_token(token)
        except OIDCVerificationError as exc:
            logger.warning("OIDC bearer token rejected: %s", exc)
            await record_security_event(actor="auth", event="oidc_token_rejected", detail=str(exc))
            raise HTTPException(401, "OIDC bearer token rejected.") from exc
        username = claims.get(settings.oidc_username_claim)
        if not username:
            raise HTTPException(
                401,
                f"OIDC token verified but has no {settings.oidc_username_claim!r} claim "
                "to match against a registered reviewer.",
            )
        async with session_scope() as session:
            reviewer = await session.scalar(select(Reviewer).where(Reviewer.username == username))
        if reviewer is None:
            # 403, not 401: authentication SUCCEEDED (the IdP vouched for
            # this person) — they're just not authorized to review here.
            await record_security_event(
                actor="auth", event="oidc_identity_not_a_reviewer", detail=username
            )
            raise HTTPException(403, f"{username!r} is not a registered reviewer.")
        return AuthenticatedReviewer(
            reviewer=reviewer, auth_method="oidc", oidc_subject=claims.get("sub")
        )

    reviewer = await require_reviewer_token(x_reviewer_token)
    return AuthenticatedReviewer(reviewer=reviewer, auth_method="token")
