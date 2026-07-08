"""Optional Telegram notifications for pending sensitive-action approvals
(Settings.telegram_bot_token — see app/api/main.py's /telegram/webhook).

A real reviewer links their account once, by messaging the bot `/start
<their reviewer token>` — the bot verifies that token against the
`reviewers` table and stores the resulting chat_id on that exact Reviewer
row (Reviewer.telegram_chat_id). From then on, every sensitive approval
they're entitled to decide (app/api/rbac.py's find_entitled_reviewers)
gets pushed to their chat with inline Approve/Reject buttons, and tapping
one calls the SAME decide_approval logic the dashboard uses — Telegram is
just another authenticated entry point into the existing approval flow,
never a parallel/weaker one.

Deliberately real-reviewers-only: the public demo reviewer (see
app/api/main.py's _ensure_demo_reviewer) is never linkable here, so public
demo approval traffic can never reach a real person's personal chat.

Fully additive and opt-in: with TELEGRAM_BOT_TOKEN unset, every function
in this module is a no-op and the dashboard-only approval flow is
completely unaffected — this was true before this module existed and
stays true after.
"""

import logging

import httpx

from app.config import get_settings
from app.db.models import Approval, Reviewer

logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"


def _api_url(method: str) -> str:
    token = get_settings().telegram_bot_token
    return f"{_TELEGRAM_API_BASE}/bot{token}/{method}"


def _approval_message_text(approval: Approval) -> str:
    username = approval.tool_args.get("username", "")
    lines = [
        f"🔐 <b>Approval needed</b> — ticket #{approval.ticket_id}",
        f"<b>Action:</b> <code>{approval.tool_name}</code>",
    ]
    if username:
        lines.append(f"<b>Target:</b> {username}")
    if approval.reasoning:
        lines.append(f"<b>Reasoning:</b> {approval.reasoning}")
    return "\n".join(lines)


def _decision_callback_data(approval_id: int, approve: bool) -> str:
    # Telegram caps callback_data at 64 bytes — this stays well under that
    # regardless of how large approval_id gets, unlike e.g. round-tripping
    # the full tool_args JSON blob would.
    return f"decide:{approval_id}:{1 if approve else 0}"


def parse_decision_callback_data(data: str) -> tuple[int, bool] | None:
    """Inverse of _decision_callback_data. Returns None for anything that
    doesn't match the expected shape — e.g. a stale/malformed callback from
    a bot restart with a different token, which must be ignored rather than
    crash the webhook handler.
    """
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "decide":
        return None
    try:
        approval_id = int(parts[1])
    except ValueError:
        return None
    if parts[2] not in ("0", "1"):
        return None
    return approval_id, parts[2] == "1"


async def notify_reviewers_of_pending_approval(session, approval: Approval, reviewers: list[Reviewer]) -> None:
    """Sends the approval notification to every reviewer in `reviewers` who
    has linked a Telegram chat_id — silently skips anyone who hasn't (most
    reviewers, until they opt in via /start). Caller (app/agent/graph.py's
    await_approval_node) is responsible for resolving `reviewers` via
    app.api.rbac.find_entitled_reviewers first.

    Best-effort: a Telegram API failure (bad token, network blip, chat
    deleted) is logged and swallowed, never allowed to fail the ticket run
    itself — a notification is a convenience layer on top of the real
    approval flow, not a dependency of it.
    """
    token = get_settings().telegram_bot_token
    if not token:
        return

    linked = [r for r in reviewers if r.telegram_chat_id]
    if not linked:
        return

    text = _approval_message_text(approval)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": _decision_callback_data(approval.id, True)},
                {"text": "❌ Reject", "callback_data": _decision_callback_data(approval.id, False)},
            ]
        ]
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        for reviewer in linked:
            try:
                resp = await client.post(
                    _api_url("sendMessage"),
                    json={
                        "chat_id": reviewer.telegram_chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "reply_markup": keyboard,
                    },
                )
                resp.raise_for_status()
            except Exception:
                logger.exception(
                    "Failed to send Telegram approval notification to reviewer %s (approval %d)",
                    reviewer.username, approval.id,
                )


async def send_decision_confirmation(chat_id: str, *, approval_id: int, approved: bool, detail: str) -> None:
    """Best-effort follow-up message after a Telegram-driven decide_approval
    call completes — confirms what happened (including failure, e.g. the
    approval was already decided by someone else in the meantime) back to
    the reviewer who tapped the button, since Telegram's own callback-query
    ack is just a tiny toast, not a real message in the chat history.
    """
    token = get_settings().telegram_bot_token
    if not token:
        return
    verb = "Approved" if approved else "Rejected"
    text = f"{'✅' if approved else '❌'} {verb} — approval #{approval_id}\n{detail}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(_api_url("sendMessage"), json={"chat_id": chat_id, "text": text})
            resp.raise_for_status()
        except Exception:
            logger.exception("Failed to send Telegram decision confirmation to chat %s", chat_id)


async def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """Dismisses the tiny loading spinner Telegram shows on the tapped
    button — required by the Bot API within a few seconds of the callback,
    regardless of how long the actual decide_approval call takes.
    """
    token = get_settings().telegram_bot_token
    if not token:
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                _api_url("answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text},
            )
            resp.raise_for_status()
        except Exception:
            logger.exception("Failed to answer Telegram callback query %s", callback_query_id)
