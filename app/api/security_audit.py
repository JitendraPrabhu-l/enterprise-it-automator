"""Security-event audit logging — reuses the existing AuditLog table (see
app/db/models.py) for auth failures and identity-binding events, not just
tool invocations. Same table, same admin-facing query surface
(GET /tickets/{id}/audit, GET /audit/export), so a security review doesn't
need a second log store to reason about "who tried what."

record_security_event() is deliberately best-effort and uses its OWN
session (not whatever session the caller might be mid-request with) —
callers invoke this from auth dependencies that are about to raise an
HTTPException, and from a webhook handler that must keep responding to
Telegram regardless of audit-write outcome. A logging failure must never
mask or replace the real auth/webhook error, and must never roll back
alongside the request it's reporting on.
"""

import logging

from app.db.models import AuditLog
from app.db.session import session_scope

logger = logging.getLogger(__name__)


async def record_security_event(*, actor: str, event: str, detail: str = "", success: bool = False) -> None:
    """actor identifies the event category (e.g. "auth", "telegram_link"),
    event is the specific thing that happened (e.g. "invalid_api_key") —
    stored in AuditLog's existing actor/tool_name columns so this reuses
    the current schema and admin query surface without a migration.
    ticket_id is always None here: these events aren't scoped to a ticket.
    """
    try:
        async with session_scope() as session:
            session.add(
                AuditLog(
                    ticket_id=None,
                    actor=actor,
                    tool_name=event,
                    tool_args={},
                    result=detail,
                    success=success,
                )
            )
    except Exception:
        logger.exception("Failed to record security event actor=%s event=%s", actor, event)
