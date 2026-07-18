"""Tamper-evident audit log: a software-layer hash chain over AuditLog rows.

Every AuditLog row is chained to the one before it (entry_hash covers
prev_hash plus this row's own fields), and the singleton AuditChainHead row
tracks the chain's current tip. append_audit_log() is the single intended
writer of AuditLog rows — every existing call site (app/mcp_server/tools.py's
_audit, app/agent/sla_sweep.py's two sweeps, app/api/security_audit.py's
record_security_event) goes through it, so there is exactly one place the
chain can be extended, and exactly one place that can get it wrong.

What this catches: an AuditLog row edited or deleted after the fact —
verify_audit_chain() recomputes every row's hash from its own stored fields
and the PRECEDING row's stored hash, so an edited `result`/`success`/etc.
value no longer reproduces the hash stored alongside it, and a deleted
trailing row leaves AuditChainHead pointing at a hash no remaining row
produces.

What this does NOT catch: a DB-admin-level actor rewriting the entire chain
from some point forward and recomputing every later hash consistently —
software-layer hash-chaining detects tampering by someone who doesn't also
rewrite the rest of the chain; it is not WORM (write-once-read-many)
storage. True immutability needs an infra-level control (S3 Object Lock, an
append-only external log) outside this database. See docs/RUNBOOKS.md's
"Audit log integrity" section for this caveat and the recommended defense
in depth (streaming to an external SIEM) — documented, not built, the same
honesty as every other right-sized tradeoff in this codebase.
"""

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditChainHead, AuditLog

logger = logging.getLogger(__name__)

_CHAIN_HEAD_ID = 1
# The hash chain's starting point — an all-zero sentinel rather than empty
# string/None, so "this is the first entry in the chain" is an explicit,
# unambiguous value stored on that first row's prev_hash, not a special case
# every reader has to know to treat differently.
_GENESIS_HASH = "0" * 64


def _compute_entry_hash(
    prev_hash: str,
    ticket_id: int | None,
    actor: str,
    tool_name: str,
    tool_args: dict[str, Any],
    result: str,
    success: bool,
) -> str:
    payload = json.dumps(
        {
            "prev_hash": prev_hash,
            "ticket_id": ticket_id,
            "actor": actor,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "result": result,
            "success": success,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def append_audit_log(
    session: AsyncSession,
    *,
    ticket_id: int | None,
    actor: str,
    tool_name: str,
    tool_args: dict[str, Any],
    result: str,
    success: bool,
) -> AuditLog:
    """The single choke point for writing an AuditLog row — computes and
    stores this entry's position in the hash chain, then advances the
    chain-head pointer in the SAME transaction. Locks the chain-head row
    (with_for_update=True: SELECT ... FOR UPDATE on Postgres, serializing
    concurrent appends across replicas; a harmless no-op on SQLite, which
    is already serialized by its own single-writer lock) so two concurrent
    audit writes can't both read the same prev_hash and silently fork the
    chain into two branches that both look valid in isolation.

    Callers add the row to `session` exactly as before (this function does
    NOT commit) — commit-timing (e.g. app/mcp_server/tools.py's
    commit_immediately on rejection paths) stays each call site's own
    concern, unchanged by this refactor.
    """
    head = await session.get(AuditChainHead, _CHAIN_HEAD_ID, with_for_update=True)
    prev_hash = head.latest_hash if head is not None else _GENESIS_HASH

    entry_hash = _compute_entry_hash(prev_hash, ticket_id, actor, tool_name, tool_args, result, success)

    row = AuditLog(
        ticket_id=ticket_id,
        actor=actor,
        tool_name=tool_name,
        tool_args=tool_args,
        result=result,
        success=success,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    session.add(row)

    if head is None:
        session.add(AuditChainHead(id=_CHAIN_HEAD_ID, latest_hash=entry_hash))
    else:
        head.latest_hash = entry_hash

    return row


async def verify_audit_chain(session: AsyncSession) -> tuple[bool, str]:
    """Walks every AuditLog row in id order, recomputing each entry_hash
    from its own stored fields and the PRECEDING row's stored (not
    recomputed) hash — so a single tampered row is reported at the row it
    was tampered, instead of every later row also mismatching and burying
    the actual point of tampering. Rows with entry_hash IS NULL predate
    hash-chaining (see AuditLog.entry_hash's docstring) and are skipped,
    resetting the expected prev_hash back to genesis for whatever
    hash-chained row follows — that first chained row's own prev_hash was
    always written as genesis (see append_audit_log's `head is None`
    branch), regardless of how many legacy rows preceded it.

    Also compares the final computed hash against AuditChainHead.latest_hash
    — the only way to catch a DELETED trailing row: removing rows leaves no
    mismatched row to find by walking what remains, but the head then
    points at a hash nothing remaining reproduces.

    Returns (True, "ok") or (False, <human-readable reason>).
    """
    rows = (await session.scalars(select(AuditLog).order_by(AuditLog.id))).all()

    prev_hash = _GENESIS_HASH
    for row in rows:
        if row.entry_hash is None:
            prev_hash = _GENESIS_HASH
            continue
        if row.prev_hash != prev_hash:
            return False, f"audit_log row {row.id}: prev_hash does not match the preceding row's hash"
        expected = _compute_entry_hash(
            prev_hash, row.ticket_id, row.actor, row.tool_name, row.tool_args, row.result, row.success
        )
        if expected != row.entry_hash:
            return False, f"audit_log row {row.id}: stored entry_hash does not match its recomputed contents"
        prev_hash = row.entry_hash

    head = await session.get(AuditChainHead, _CHAIN_HEAD_ID)
    expected_head = head.latest_hash if head is not None else _GENESIS_HASH
    if prev_hash != expected_head:
        return False, "audit chain head does not match the last row's hash — trailing rows may have been deleted"

    return True, "ok"
