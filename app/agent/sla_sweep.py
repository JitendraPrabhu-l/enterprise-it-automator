"""Approval SLA timeout + stuck-ticket detection (Stage 4.5, scoped down —
see ROADMAP.md: a full Temporal/APScheduler deployment is overkill for a
single periodic sweep, so this is a plain asyncio background loop started
from FastAPI's lifespan).

Reconciles two findings from the roadmap into one mechanism: "what happens
if nobody ever clicks approve" (an Approval sitting PENDING past its
sla_deadline) and "what happens if a ticket gets stuck mid-run" (a Ticket
sitting in PLANNING/EXECUTING far longer than any real run should take,
e.g. because a crash lost track of it). Both are surfaced the same way —
flip a status, write an audit trail entry, log it — rather than silently
sitting there forever with no operator visibility.
"""

import asyncio
import datetime as dt
import logging

from sqlalchemy import select

from app.api.idempotency import purge_expired
from app.config import get_settings
from app.db.models import AuditLog, Approval, ApprovalStatus, Ticket, TicketStatus
from app.db.session import session_scope

logger = logging.getLogger(__name__)

# A ticket sitting in PLANNING/EXECUTING for longer than this is almost
# certainly stuck (crashed mid-run, orphaned by a process restart before the
# checkpointer resumed it) rather than genuinely still in progress — normal
# runs complete in seconds to low minutes even with HITL pauses removed from
# the clock (an approval wait moves the ticket to AWAITING_APPROVAL, not
# EXECUTING, so it isn't penalized by slow human reviewers).
STUCK_TICKET_THRESHOLD_MINUTES = 30


async def sweep_overdue_approvals() -> list[int]:
    """Escalates every PENDING approval whose sla_deadline has passed.
    Escalation here means: mark ESCALATED (not auto-approved or
    auto-rejected — a sensitive action should never execute or get
    permanently blocked without a human decision) and write an audit trail
    entry so it's visible in the same place every other action is. Returns
    the list of escalated approval IDs, mainly for tests/logging.
    """
    now = dt.datetime.now(dt.timezone.utc)
    escalated_ids: list[int] = []

    async with session_scope() as session:
        overdue = await session.scalars(
            select(Approval).where(
                Approval.status == ApprovalStatus.PENDING,
                Approval.sla_deadline < now,
            )
        )
        for approval in overdue:
            approval.status = ApprovalStatus.ESCALATED
            approval.escalated_at = now
            session.add(
                AuditLog(
                    ticket_id=approval.ticket_id,
                    actor="sla_sweep",
                    tool_name=approval.tool_name,
                    tool_args=approval.tool_args,
                    result=(
                        f"Approval {approval.id} escalated — pending past its "
                        f"SLA deadline ({approval.sla_deadline.isoformat()}) with no reviewer decision."
                    ),
                    success=False,
                )
            )
            escalated_ids.append(approval.id)

    if escalated_ids:
        logger.warning("SLA sweep escalated %d overdue approval(s): %s", len(escalated_ids), escalated_ids)
    return escalated_ids


async def sweep_stuck_tickets() -> list[int]:
    """Flags tickets stuck in PLANNING/EXECUTING well past a normal run's
    duration — these are the ones a crash or an unhandled exception outside
    the graph's own error handling could have silently abandoned. Doesn't
    change ticket.status (a human should look at *why* before deciding),
    just writes an audit entry so it surfaces in the same operator-visible
    trail as everything else.
    """
    threshold = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=STUCK_TICKET_THRESHOLD_MINUTES)
    stuck_ids: list[int] = []

    async with session_scope() as session:
        stuck = await session.scalars(
            select(Ticket).where(
                Ticket.status.in_([TicketStatus.PLANNING, TicketStatus.EXECUTING]),
                Ticket.updated_at < threshold,
            )
        )
        for ticket in stuck:
            session.add(
                AuditLog(
                    ticket_id=ticket.id,
                    actor="sla_sweep",
                    tool_name="stuck_ticket_detection",
                    tool_args={},
                    result=(
                        f"Ticket {ticket.id} has been in {ticket.status.value!r} since "
                        f"{ticket.updated_at.isoformat()}, past the "
                        f"{STUCK_TICKET_THRESHOLD_MINUTES}-minute stuck-ticket threshold."
                    ),
                    success=False,
                )
            )
            stuck_ids.append(ticket.id)

    if stuck_ids:
        logger.warning("SLA sweep flagged %d stuck ticket(s): %s", len(stuck_ids), stuck_ids)
    return stuck_ids


async def _purge_expired_idempotency_keys() -> int:
    """Deletes IdempotencyKey rows past their TTL. Piggybacks on this sweep's
    existing cadence rather than a separate task, since store_response()
    (app/api/idempotency.py) writes a row on every Idempotency-Key-bearing
    POST /tickets with no automatic expiry of its own — this is the only
    thing that actually deletes them.
    """
    async with session_scope() as session:
        return await purge_expired(session)


async def run_sla_sweep() -> dict:
    """One full sweep pass — every housekeeping check this app needs run
    periodically, together, since they share the same cadence and the same
    "surface it, don't act on it destructively" policy.
    """
    escalated = await sweep_overdue_approvals()
    stuck = await sweep_stuck_tickets()
    purged = await _purge_expired_idempotency_keys()
    if purged:
        logger.info("SLA sweep purged %d expired idempotency key(s)", purged)
    return {"escalated_approvals": escalated, "stuck_tickets": stuck}


async def sla_sweep_loop() -> None:
    """Background task entry point: runs run_sla_sweep() forever on the
    configured interval. Started as an asyncio task from the app's lifespan
    and cancelled on shutdown — a plain loop rather than APScheduler/Celery,
    since one periodic job doesn't warrant a scheduling framework dependency
    (see ROADMAP.md's Stage 4 trap notes).
    """
    interval = get_settings().sla_sweep_interval_seconds
    while True:
        try:
            await run_sla_sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SLA sweep pass failed — will retry next interval")
        await asyncio.sleep(interval)
