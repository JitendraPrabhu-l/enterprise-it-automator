import pytest
from fastapi import HTTPException

from app.api.idempotency import get_cached_response, store_response


async def test_get_cached_response_returns_none_for_unseen_key(session):
    result = await get_cached_response(session, "key-1", {"a": 1})
    assert result is None


async def test_store_then_get_returns_stored_response(session):
    await store_response(session, "key-1", {"a": 1}, {"ticket_id": 42, "done": True})
    await session.commit()

    result = await get_cached_response(session, "key-1", {"a": 1})
    assert result == {"ticket_id": 42, "done": True}


async def test_same_key_different_payload_raises_409(session):
    await store_response(session, "key-1", {"a": 1}, {"ticket_id": 42, "done": True})
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await get_cached_response(session, "key-1", {"a": 2})
    assert exc.value.status_code == 409


async def test_same_key_same_payload_different_key_ordering_still_matches(session):
    """Request hashing must be order-independent (dict key order shouldn't
    matter) since JSON payloads with identical content can serialize with
    different key orders depending on the client."""
    await store_response(session, "key-1", {"a": 1, "b": 2}, {"ticket_id": 42, "done": True})
    await session.commit()

    result = await get_cached_response(session, "key-1", {"b": 2, "a": 1})
    assert result == {"ticket_id": 42, "done": True}


async def test_different_keys_are_independent(session):
    await store_response(session, "key-1", {"a": 1}, {"ticket_id": 1})
    await store_response(session, "key-2", {"a": 1}, {"ticket_id": 2})
    await session.commit()

    assert await get_cached_response(session, "key-1", {"a": 1}) == {"ticket_id": 1}
    assert await get_cached_response(session, "key-2", {"a": 1}) == {"ticket_id": 2}
