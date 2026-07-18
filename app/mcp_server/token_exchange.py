"""Self-issued, audience/scope-scoped MCP bearer tokens — the token-exchange
half of right-sizing OAuth 2.1 for this project's HTTP transport.

What this is: `MCP_SERVER_TOKEN` (the existing static shared secret) stays
the ADMIN credential — full access, unchanged from before this existed —
and becomes the ONLY credential that can call `POST /token/exchange` to
mint a short-lived, domain-scoped JWT. `app/agent/mcp_client.py` uses that
scoped token for actual tool calls over HTTP instead of sending the raw
admin secret on every request, so a leaked scoped token (logged, cached,
proxied through an intermediary) authorizes one domain for minutes, not
the whole gateway forever. `app/mcp_server/server.py`'s
`_BearerTokenMiddleware` is what actually enforces a scoped token's domain
coverage against the tool being called (RFC 8707-style audience/resource
scoping, applied per MCP tool-call rather than per HTTP resource).

What this deliberately is NOT (see README.md's "Securing the HTTP
transport for real deployment" section for the full right-sizing
rationale, same shape as scoping down full Keycloak/OIDC for reviewer
identity): no external IdP, no user-facing authorization-code/PKCE flow,
no RFC 9728 Protected Resource Metadata discovery document. This is
machine-to-machine token exchange between two processes that already share
a trust root (MCP_SERVER_TOKEN) — narrowing what a derived credential can
do, not establishing trust from nothing.

HS256, signed with MCP_SERVER_TOKEN itself rather than a separate signing
secret: the admin token is already this transport's trust root, so a JWT
signed with it is exactly "a strictly narrower, time-limited credential
derived from the same root," not a second secret to separately provision,
distribute, and rotate.
"""

import time
from typing import Any

import jwt

from app.config import get_settings

AUDIENCE = "mcp-gateway"
ISSUER = "mcp-gateway"


class InvalidScopedTokenError(Exception):
    """Raised for any invalid, expired, or malformed scoped token."""


def mint_scoped_token(scopes: list[str], ttl_seconds: int | None = None) -> dict[str, Any]:
    """Mints an RFC 6749 §5.1-shaped token response (access_token/token_type/
    expires_in/scope) for the given domain scopes, signed with the current
    MCP_SERVER_TOKEN. Callers (POST /token/exchange) are responsible for
    having already verified the caller presented the raw admin token before
    calling this — this function itself does no authorization, only minting.
    """
    settings = get_settings()
    key = settings.mcp_server_token
    if not key:
        raise RuntimeError("MCP_SERVER_TOKEN must be set to mint scoped tokens.")
    ttl = ttl_seconds if ttl_seconds is not None else settings.mcp_scoped_token_ttl_seconds
    now = int(time.time())
    scope_str = " ".join(sorted(set(scopes)))
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "scope": scope_str,
        "iat": now,
        "exp": now + ttl,
    }
    token = jwt.encode(payload, key, algorithm="HS256")
    return {"access_token": token, "token_type": "Bearer", "expires_in": ttl, "scope": scope_str}


def verify_scoped_token(token: str) -> set[str]:
    """Verifies signature/audience/issuer/expiry against the CURRENT
    MCP_SERVER_TOKEN (re-read via get_settings() each call, not cached —
    consistent with server.py's _authenticated_streamable_http_app already
    re-deriving from live settings, e.g. so a test that rotates the admin
    token mid-process gets the token it actually configured). Raises
    InvalidScopedTokenError on any failure; returns the token's scope set
    (possibly empty) on success.
    """
    settings = get_settings()
    key = settings.mcp_server_token
    try:
        claims = jwt.decode(token, key, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER)
    except jwt.InvalidTokenError as exc:
        raise InvalidScopedTokenError(str(exc)) from exc
    scope = claims.get("scope", "")
    return set(scope.split()) if scope else set()
