"""Tests for app/notifications/telegram.py — the optional Telegram
integration that lets a real reviewer link their account and decide
sensitive-action approvals with inline buttons, instead of only through
the dashboard.

httpx calls are monkeypatched at the module's AsyncClient usage rather than
hitting the real Telegram API — these tests verify OUR request-shaping and
callback-data encoding/decoding logic, not Telegram's own API behavior.
"""

import pytest

from app.notifications.telegram import (
    _decision_callback_data,
    parse_decision_callback_data,
)


def test_decision_callback_data_roundtrips_approve():
    data = _decision_callback_data(42, True)
    assert parse_decision_callback_data(data) == (42, True)


def test_decision_callback_data_roundtrips_reject():
    data = _decision_callback_data(42, False)
    assert parse_decision_callback_data(data) == (42, False)


def test_decision_callback_data_stays_under_telegram_64_byte_limit():
    data = _decision_callback_data(999_999_999, True)
    assert len(data.encode()) <= 64


@pytest.mark.parametrize("garbage", ["", "not:a:real:thing:at:all", "decide:abc:1", "decide:1:2", "approve:1"])
def test_parse_decision_callback_data_rejects_malformed_input(garbage):
    """Must return None, not raise — a malformed/stale callback_data (e.g.
    from a bot restart with a different token in some other deployment)
    must be ignorable, not crash the webhook handler."""
    assert parse_decision_callback_data(garbage) is None


async def test_notify_reviewers_is_noop_without_token(monkeypatch):
    from app.config import get_settings
    from app.notifications.telegram import notify_reviewers_of_pending_approval

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    get_settings.cache_clear()

    calls = []

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            calls.append((a, k))

    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeClient())

    await notify_reviewers_of_pending_approval(None, object(), [])
    assert calls == []
    get_settings.cache_clear()


async def test_notify_reviewers_skips_unlinked_reviewers(monkeypatch):
    from app.config import get_settings
    from app.db.models import ApprovalStatus, ReviewerRole
    from app.notifications.telegram import notify_reviewers_of_pending_approval

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    get_settings.cache_clear()

    class _FakeReviewer:
        def __init__(self, username, chat_id):
            self.username = username
            self.telegram_chat_id = chat_id

    class _FakeApproval:
        id = 1
        ticket_id = 7
        tool_name = "disable_user"
        tool_args = {"username": "jsmith"}
        reasoning = "test"
        status = ApprovalStatus.PENDING

    calls = []

    class _FakeResponse:
        def raise_for_status(self):
            pass

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            calls.append(json)
            return _FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeClient())

    reviewers = [
        _FakeReviewer("unlinked-reviewer", None),
        _FakeReviewer("linked-reviewer", "12345"),
    ]
    _ = ReviewerRole  # imported for readability of the fixture above

    await notify_reviewers_of_pending_approval(None, _FakeApproval(), reviewers)

    assert len(calls) == 1
    assert calls[0]["chat_id"] == "12345"
    assert "disable_user" in calls[0]["text"]
    get_settings.cache_clear()


async def test_notify_reviewers_swallows_send_failures(monkeypatch):
    """A Telegram API failure must never propagate — it's a best-effort
    convenience layer on top of the real (dashboard) approval flow."""
    from app.config import get_settings
    from app.db.models import ApprovalStatus
    from app.notifications.telegram import notify_reviewers_of_pending_approval

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    get_settings.cache_clear()

    class _FakeReviewer:
        username = "linked-reviewer"
        telegram_chat_id = "12345"

    class _FakeApproval:
        id = 1
        ticket_id = 7
        tool_name = "disable_user"
        tool_args = {"username": "jsmith"}
        reasoning = ""
        status = ApprovalStatus.PENDING

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise ConnectionError("network blip")

    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeClient())

    await notify_reviewers_of_pending_approval(None, _FakeApproval(), [_FakeReviewer()])  # must not raise
    get_settings.cache_clear()
