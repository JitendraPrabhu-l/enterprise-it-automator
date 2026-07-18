"""Per-ticket LLM token budget — the cost-control half of running an agent
loop against a metered model. MAX_REPLANS already bounds how many times the
planner can run; this bounds the run in the currency that actually gets
billed (tokens), which matters because a few replans over a huge tool
reference/progress summary can cost more than many replans over a small one.

Mechanics: runner.start_ticket_run / resume_ticket_run install a
run-scoped accumulator (a ContextVar holding a mutable cell, so the graph's
nodes and the observability layer all see the same counter for THIS run and
concurrent ticket runs never share one). app/observability.py's
record_llm_call — the single choke point every LLM response already flows
through — feeds usage into it; plan/replan nodes persist the running total
into AgentState (so it checkpoints across HITL interrupts: the budget is
per-TICKET, not per-HTTP-request) and refuse to invoke the planner again
once the budget is spent, failing the ticket with an explicit error instead
of quietly continuing to spend.

Disabled by default (MAX_TOKENS_PER_TICKET=0) — a deliberate opt-in, since
the right ceiling is deployment-specific (model, prompt sizes, replan
budget) and a wrong-by-default ceiling would fail legitimate tickets.

Org-level cost governance (MAX_TOKENS_PER_CLIENT_PER_DAY /
MAX_ORG_TOKENS_PER_DAY, both also 0-disabled by default) lives in this same
module: a per-ApiClient and an org-wide DAILY token budget, on top of this
per-ticket ceiling. record_client_spend_and_check_budget() is called from
the same plan/replan checkpoints that already persist tokens_used into
AgentState, attributing the DELTA since the last call to whichever
ApiClient's Ticket.submitted_by_client_id this ticket belongs to — looked
up fresh from the DB by ticket_id each call rather than threaded through
AgentState/the checkpointer, since a ticket's owning client never changes
after creation, so a per-call lookup is exactly as correct as a cached one
without any checkpoint schema change.
"""

import datetime as dt
import logging
from contextvars import ContextVar

from sqlalchemy import func, select

from app import metrics
from app.config import get_settings
from app.db.models import ApiClient, Ticket, utcnow
from app.db.session import session_scope

logger = logging.getLogger(__name__)

# A one-element list rather than a bare int: ContextVar values are
# immutable-per-set, but every party (nodes, record_llm_call) must see each
# other's increments within one run — a shared mutable cell does that
# without re-set() gymnastics at every call site.
_run_tokens: ContextVar[list[int] | None] = ContextVar("_run_tokens", default=None)

# How much of _run_tokens' running total has already been attributed to an
# ApiClient's daily counter THIS run — seeded to the same initial value as
# _run_tokens on start_accounting(), so record_client_spend_and_check_budget
# only ever attributes tokens spent since accounting started (or since the
# last resume), never re-attributing a resumed ticket's pre-interrupt spend
# a second time.
_last_attributed: ContextVar[list[int] | None] = ContextVar("_last_attributed", default=None)


def start_accounting(initial_tokens: int = 0) -> None:
    """Installs this run's accumulator, seeded with the tokens the ticket
    has already spent (0 for a fresh run; the checkpointed state's
    tokens_used on resume — that's what makes the budget span interrupts).
    ContextVar scoping means each concurrent ticket run gets its own cell.
    """
    _run_tokens.set([initial_tokens])
    _last_attributed.set([initial_tokens])


def add_tokens(count: int) -> None:
    """Called by record_llm_call for every LLM response. Outside a ticket
    run (no accumulator installed — e.g. unit tests calling an LLM helper
    directly) this is a no-op rather than an error: token accounting is a
    ticket-run concern, not a precondition for calling an LLM.
    """
    cell = _run_tokens.get()
    if cell is not None and count > 0:
        cell[0] += count


def current_total() -> int | None:
    """This run's spend so far, or None when no accumulator is installed."""
    cell = _run_tokens.get()
    return cell[0] if cell is not None else None


def budget_exceeded() -> bool:
    """True when a budget is configured AND this run's accumulator says
    it's spent. With MAX_TOKENS_PER_TICKET=0 (default) or outside a run,
    always False — exactly the pre-budget behavior.
    """
    limit = get_settings().max_tokens_per_ticket
    if limit <= 0:
        return False
    total = current_total()
    return total is not None and total >= limit


def budget_error_message(ticket_id: int) -> str:
    limit = get_settings().max_tokens_per_ticket
    return (
        f"Ticket {ticket_id} exceeded its LLM token budget "
        f"({current_total()} used, limit {limit}) — aborting before further planner "
        "calls. Raise MAX_TOKENS_PER_TICKET or investigate why this ticket "
        "loops (see the replan history in the audit trail)."
    )


def _org_governance_configured() -> bool:
    settings = get_settings()
    return settings.max_tokens_per_client_per_day > 0 or settings.max_org_tokens_per_day > 0


async def _reset_if_new_day(client: ApiClient, now: dt.datetime) -> None:
    reset_at = client.token_count_reset_at
    if reset_at is None:
        client.tokens_used_today = 0
        client.token_count_reset_at = now
        return
    # SQLite doesn't reliably round-trip tzinfo on DateTime(timezone=True)
    # columns — same caveat as app/api/main.py's daily request-count check.
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=dt.timezone.utc)
    if now - reset_at > dt.timedelta(days=1):
        client.tokens_used_today = 0
        client.token_count_reset_at = now


async def check_client_org_budget(session, client_id: int | None) -> str | None:
    """Fresh-reads the client's and org's current daily token totals and
    returns a human-readable reason if either configured cap is already
    met or exceeded, else None. Shared by the pre-submission check
    (app/api/main.py, before a ticket/graph run even starts) and the
    runtime gate below (mid-run, after new spend has just been recorded) —
    same check, two call sites, same reasoning as
    app/api/main.py's daily REQUEST-count cap having both a submission-time
    and (via MAX_TOKENS_PER_TICKET) a runtime enforcement point.
    """
    if client_id is None or not _org_governance_configured():
        return None
    settings = get_settings()

    client = await session.get(ApiClient, client_id)
    client_total = (client.tokens_used_today or 0) if client is not None else 0
    if settings.max_tokens_per_client_per_day > 0 and client_total >= settings.max_tokens_per_client_per_day:
        return (
            f"Daily token budget ({settings.max_tokens_per_client_per_day}/day) "
            "reached for this API client. Try again after the daily reset."
        )

    if settings.max_org_tokens_per_day > 0:
        # Approximate on purpose: each client's own "today" resets on its
        # own read (_reset_if_new_day), so summing raw tokens_used_today
        # across clients whose reset windows don't perfectly align is a
        # slight over/under-count right at a UTC day boundary — the same
        # right-sized approximation this codebase already accepts for
        # daily_request_count, not worth a global-midnight-reset cron job.
        org_total = (await session.scalar(select(func.sum(ApiClient.tokens_used_today)))) or 0
        metrics.ORG_TOKENS_TODAY.set(org_total)
        metrics.ORG_TOKEN_BUDGET_LIMIT.set(settings.max_org_tokens_per_day)
        if org_total >= settings.max_org_tokens_per_day:
            return f"Org-wide daily token budget ({settings.max_org_tokens_per_day}/day) reached."

    return None


async def record_client_spend_and_check_budget(ticket_id: int) -> str | None:
    """Attributes this run's NEW token spend (since the last call, or since
    start_accounting()) to whichever ApiClient owns this ticket, then
    checks whether that client's or the org's daily budget is now
    exceeded. No-op (returns None immediately) if neither cap is
    configured, outside a ticket run, or the ticket has no attributable
    client (API_KEY unset / local demo mode) — same shape as
    app/api/main.py's daily-request-count check being a no-op for
    client=None.
    """
    if not _org_governance_configured():
        return None
    cell = _last_attributed.get()
    if cell is None:
        return None
    total = current_total() or 0
    delta = total - cell[0]
    if delta <= 0:
        return None

    async with session_scope() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None or ticket.submitted_by_client_id is None:
            return None
        client_id = ticket.submitted_by_client_id
        client = await session.get(ApiClient, client_id)
        if client is None:
            return None
        # Commit the attribution now, even though the budget check below may
        # still report exceeded — this delta genuinely was spent and must
        # not be re-attributed the next time this function is called.
        cell[0] = total
        await _reset_if_new_day(client, utcnow())
        client.tokens_used_today = (client.tokens_used_today or 0) + delta
        return await check_client_org_budget(session, client_id)
