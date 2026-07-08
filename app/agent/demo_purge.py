"""Daily reset for the public demo client's own data (Settings.demo_api_key
— see app/api/main.py's _ensure_demo_guest_client).

Keeping demo traffic from mixing with real operational data has two parts,
already both in place: read-scoping (Ticket.submitted_by_client_id, so a
demo submission is never visible to any OTHER standard client) and a low
daily request cap. This module adds the third piece — the demo client's
OWN tickets/approvals/audit entries don't accumulate forever alongside real
ones; they're hard-deleted once a day, keyed off ApiClient.data_last_purged_at
so this only ever runs against the one client it's meant for, on a fixed
daily cadence, not a rolling per-row TTL.

Deliberately narrow in scope: this ONLY ever touches rows owned by the
DEMO_API_KEY client. A real ApiClient's tickets are never purged by
anything in this module — there is no general-purpose "clean up old
tickets" feature here, on purpose.
"""

import asyncio
import datetime as dt
import logging

from sqlalchemy import delete, select

from app.config import get_settings
from app.db.models import ApiClient, Approval, AuditLog, Ticket
from app.db.session import session_scope

logger = logging.getLogger(__name__)


async def _get_demo_client(session) -> ApiClient | None:
    demo_key = get_settings().demo_api_key
    if not demo_key:
        return None
    return await session.scalar(select(ApiClient).where(ApiClient.key == demo_key))


def _due_for_reset(client: ApiClient, now: dt.datetime) -> bool:
    if client.data_last_purged_at is None:
        return True
    last_purged = client.data_last_purged_at
    # Same SQLite-vs-Postgres tzinfo round-trip quirk as
    # app/api/main.py's _check_and_increment_daily_request_count — assume
    # UTC (everything this app writes to this column already is) rather
    # than compare inside a SQL WHERE clause, since this is a single-row
    # lookup by primary key, not a query.
    if last_purged.tzinfo is None:
        last_purged = last_purged.replace(tzinfo=dt.timezone.utc)
    hours = get_settings().demo_data_reset_hours
    return now - last_purged >= dt.timedelta(hours=hours)


async def reset_demo_data_if_due() -> int:
    """Hard-deletes the demo client's own tickets (and, via the FK,
    dependent approvals/audit entries — deleted explicitly and first, since
    Ticket's ORM-level cascade="all, delete-orphan" only fires when deleted
    THROUGH the ORM relationship, not via a bulk DELETE statement like this)
    if the configured reset interval has elapsed since the last purge, or
    if it's never been purged before.

    Also deletes each purged ticket's LangGraph checkpoint state (a
    separate store from the app DB — see app.agent.runner.delete_ticket_thread)
    so no orphaned checkpoint rows accumulate once the app-DB Ticket row
    they belong to is gone.

    No-op (returns 0) if DEMO_API_KEY is unset, or if the reset isn't due
    yet. Returns the number of tickets deleted.
    """
    now = dt.datetime.now(dt.timezone.utc)

    async with session_scope() as session:
        client = await _get_demo_client(session)
        if client is None:
            return 0
        if not _due_for_reset(client, now):
            return 0
        client_id = client.id
        ticket_ids = list(
            await session.scalars(select(Ticket.id).where(Ticket.submitted_by_client_id == client_id))
        )
        if ticket_ids:
            await session.execute(delete(Approval).where(Approval.ticket_id.in_(ticket_ids)))
            await session.execute(delete(AuditLog).where(AuditLog.ticket_id.in_(ticket_ids)))
            await session.execute(delete(Ticket).where(Ticket.id.in_(ticket_ids)))
        client.data_last_purged_at = now
        client.daily_request_count = 0
        client.request_count_reset_at = now

    if ticket_ids:
        from app.agent.runner import delete_ticket_thread

        for ticket_id in ticket_ids:
            try:
                await delete_ticket_thread(ticket_id)
            except Exception:
                # A checkpoint-store failure must not block the app-DB
                # purge that already committed above, and must not stop
                # the remaining tickets in this batch from being cleaned up
                # — an orphaned checkpoint row is a minor, self-contained
                # leak; losing the whole daily reset over one bad thread_id
                # would be worse.
                logger.exception("Failed to delete checkpoint thread for demo ticket %d", ticket_id)

    if ticket_ids:
        logger.info("Demo data reset: purged %d ticket(s) for the public demo client", len(ticket_ids))
    return len(ticket_ids)


# How often the background loop CHECKS whether a reset is due — much
# shorter than demo_data_reset_hours itself (the actual reset cadence).
# reset_demo_data_if_due() is a no-op unless the configured interval has
# actually elapsed, so checking hourly rather than sleeping for a full 24h
# stretch just means the reset fires within an hour of becoming due,
# rather than needing the loop itself to track a 24h-aligned wake time.
_CHECK_INTERVAL_SECONDS = 3600


async def demo_purge_loop() -> None:
    """Background task entry point: checks reset_demo_data_if_due() every
    _CHECK_INTERVAL_SECONDS forever. Started from the app's lifespan and
    cancelled on shutdown — same plain-asyncio-loop pattern as
    app/agent/sla_sweep.py's sla_sweep_loop, for the same reason (one
    periodic job doesn't warrant a scheduling framework dependency).
    """
    while True:
        try:
            await reset_demo_data_if_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Demo data reset pass failed — will retry next interval")
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
