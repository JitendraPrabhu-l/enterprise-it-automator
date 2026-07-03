"""Idempotency-key handling for POST /tickets.

A client-supplied Idempotency-Key header lets a retried request (e.g. after
a timeout where the caller doesn't know if the first attempt landed) replay
the original response instead of creating a duplicate ticket and re-running
the agent graph. The request body is hashed alongside the key: reusing a key
with a *different* payload is a client bug, not a legitimate retry, and is
rejected rather than silently serving stale data for the wrong request.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IdempotencyKey

TTL = timedelta(hours=24)


def _hash_request(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def get_cached_response(
    session: AsyncSession, key: str, payload: dict
) -> dict | None:
    """Returns the stored response dict for a previously-seen key+payload
    pair, or None if this is a fresh key. Raises 409 if the key was
    previously used with a *different* payload (misuse, not a retry)."""
    row = await session.get(IdempotencyKey, key)
    if row is None:
        return None

    if row.created_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc) - TTL:
        await session.delete(row)
        return None

    if row.request_hash != _hash_request(payload):
        raise HTTPException(
            409,
            f"Idempotency-Key {key!r} was already used with a different request body",
        )
    return json.loads(row.response_json)


async def store_response(
    session: AsyncSession, key: str, payload: dict, response: dict
) -> None:
    session.add(
        IdempotencyKey(
            key=key,
            request_hash=_hash_request(payload),
            response_json=json.dumps(response),
        )
    )


async def purge_expired(session: AsyncSession) -> int:
    """Deletes idempotency keys older than TTL. Not scheduled automatically
    (no background task infra in this project) — safe to call opportunistically
    or wire into a future scheduled sweep (see Stage 4.5's SLA sweep)."""
    cutoff = datetime.now(timezone.utc) - TTL
    result = await session.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff))
    return result.rowcount or 0
