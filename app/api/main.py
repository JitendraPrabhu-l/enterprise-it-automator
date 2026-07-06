import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from ag_ui.encoder import EventEncoder
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import select, text

from app.agent.ag_ui_bridge import stream_resume_run, stream_ticket_run
from app.agent.runner import resume_ticket_run, start_ticket_run
from app.agent.sla_sweep import run_sla_sweep, sla_sweep_loop
from app.api.auth import require_api_key, require_reviewer_token
from app.api.idempotency import get_cached_response, store_response
from app.api.rbac import ApprovalNotAuthorizedError, authorize_reviewer
from app.api.schemas import (
    ApprovalDecision,
    ApprovalOut,
    AuditLogOut,
    EmployeeOut,
    RunResult,
    SlaSweepResult,
    TicketCreate,
    TicketOut,
)
from app.config import get_settings
from app.db.models import (
    Approval,
    ApprovalStatus,
    AuditLog,
    EmployeeUser,
    Reviewer,
    Ticket,
    TicketStatus,
    UserStatus,
)
from app.db.session import init_db, session_scope
from app.logging_config import configure_logging, set_request_id
from app.observability import configure_observability

configure_logging()
configure_observability()
logger = logging.getLogger(__name__)

# Bounds blast radius on the two LLM-driven mutating endpoints — a retry
# loop, a misconfigured client, or automation feeding the planner shouldn't
# be able to spin up unbounded concurrent agent runs / LLM spend. Keyed by
# client IP (not the shared API key, which today is one value for every
# caller and wouldn't distinguish between them) — see Stage 4 for real
# per-user identity, which would make key-based limiting meaningful.
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not get_settings().api_key:
        logger.warning(
            "API_KEY is not set — /tickets, /approvals, and /audit endpoints are "
            "UNAUTHENTICATED. Set API_KEY in .env before exposing this beyond localhost."
        )
    await init_db()
    sla_sweep_task = asyncio.create_task(sla_sweep_loop())
    yield
    sla_sweep_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sla_sweep_task
    from app.agent import runner

    if runner._checkpointer_cm is not None:
        await runner._checkpointer_cm.__aexit__(None, None, None)


app = FastAPI(
    title="MCP-Enabled Enterprise IT Automator",
    description=(
        "LangGraph agent that automates employee onboarding/offboarding via a "
        "custom MCP server, with human-in-the-loop approval for sensitive actions."
    ),
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    set_request_id(request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    set_request_id(None)
    return response


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/health")
async def health() -> dict:
    from app.mcp_server.circuit_breaker import snapshot_all_breakers

    return {"status": "ok", "mcp_domains": snapshot_all_breakers()}


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe: verifies the app DB is actually reachable (not just
    that the process is up) and that the LangGraph checkpointer has been
    initialized — distinct from /health, which is a liveness check with no
    real dependency verification.
    """
    checks = {"database": False, "checkpointer": False}
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        logger.exception("Readiness check: database unreachable")

    from app.agent import runner

    checks["checkpointer"] = runner._graph is not None

    all_ready = all(checks.values())
    return JSONResponse(status_code=200 if all_ready else 503, content={"ready": all_ready, "checks": checks})


@app.get("/")
async def ui() -> FileResponse:
    """Intentionally NOT behind require_api_key: this serves only the
    static HTML/CSS/JS shell (no data, no secrets) — the page itself is
    where a visitor enters their API key/reviewer token before any actual
    data-fetching call is made. Since a plain browser navigation can't
    attach a custom X-API-Key header, gating this route would make the
    page unable to load far enough to let anyone type the key in at all.
    Every endpoint the page's JS actually calls to fetch or mutate data
    still requires the API key (and, for approval decisions, a valid
    reviewer token) — this route reveals only the app's static UI shape to
    an unauthenticated visitor, not any ticket/employee/approval data.
    """
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/tickets", response_model=RunResult, dependencies=[Depends(require_api_key)])
@limiter.limit("20/minute")
async def submit_ticket(
    request: Request, payload: TicketCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")
) -> RunResult:
    """Creates a ticket and immediately runs the agent on it up to the first
    HITL interrupt (or completion, if no sensitive actions are needed).

    If an Idempotency-Key header is supplied and was already used with this
    exact request body, replays the stored response instead of creating a
    duplicate ticket and re-running the agent graph.
    """
    payload_dict = payload.model_dump()

    if idempotency_key:
        async with session_scope() as session:
            cached = await get_cached_response(session, idempotency_key, payload_dict)
        if cached is not None:
            return RunResult(**cached)

    async with session_scope() as session:
        ticket = Ticket(
            requester=payload.requester,
            subject=payload.subject,
            body=payload.body,
            status=TicketStatus.PLANNING,
        )
        session.add(ticket)
        await session.flush()
        ticket_id = ticket.id
        ticket_text = f"Subject: {payload.subject}\n\n{payload.body}"

    result = await start_ticket_run(ticket_id, ticket_text)

    if idempotency_key:
        async with session_scope() as session:
            await store_response(session, idempotency_key, payload_dict, result)

    return RunResult(**result)


@app.post("/tickets/stream", dependencies=[Depends(require_api_key)])
@limiter.limit("20/minute")
async def submit_ticket_stream(request: Request, payload: TicketCreate) -> StreamingResponse:
    """AG-UI-protocol streaming counterpart to POST /tickets (see
    app/agent/ag_ui_bridge.py): creates the ticket the same way, then
    streams RUN_STARTED/STEP_*/TOOL_CALL_*/STATE_DELTA/RUN_FINISHED events
    over SSE as the graph actually executes, instead of blocking until the
    first interrupt or completion and returning one JSON blob.

    No Idempotency-Key support here — replaying a cached *stream* doesn't
    make sense the way replaying a cached final JSON result does; a client
    that needs idempotent ticket submission should use POST /tickets.
    """
    async with session_scope() as session:
        ticket = Ticket(
            requester=payload.requester,
            subject=payload.subject,
            body=payload.body,
            status=TicketStatus.PLANNING,
        )
        session.add(ticket)
        await session.flush()
        ticket_id = ticket.id
        ticket_text = f"Subject: {payload.subject}\n\n{payload.body}"

    encoder = EventEncoder()
    run_id = str(uuid.uuid4())

    async def event_source():
        async for event in stream_ticket_run(ticket_id, ticket_text, run_id):
            yield encoder.encode(event)

    return StreamingResponse(event_source(), media_type=encoder.get_content_type())


@app.get("/tickets", response_model=list[TicketOut], dependencies=[Depends(require_api_key)])
async def list_tickets() -> list[Ticket]:
    async with session_scope() as session:
        rows = await session.scalars(select(Ticket).order_by(Ticket.created_at.desc()))
        return list(rows)


@app.get(
    "/tickets/{ticket_id}", response_model=TicketOut, dependencies=[Depends(require_api_key)]
)
async def get_ticket(ticket_id: int) -> Ticket:
    async with session_scope() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            raise HTTPException(404, f"No such ticket: {ticket_id}")
        return ticket


@app.get(
    "/employees", response_model=list[EmployeeOut], dependencies=[Depends(require_api_key)]
)
async def list_employees(status: str | None = None) -> list[EmployeeUser]:
    """Current (active) and past (disabled) employees in the mock identity store."""
    async with session_scope() as session:
        query = select(EmployeeUser).order_by(EmployeeUser.full_name)
        if status:
            try:
                query = query.where(EmployeeUser.status == UserStatus(status))
            except ValueError:
                raise HTTPException(400, f"Invalid status: {status!r}")
        rows = await session.scalars(query)
        return list(rows)


@app.get(
    "/approvals", response_model=list[ApprovalOut], dependencies=[Depends(require_api_key)]
)
async def list_approvals(status: str | None = None) -> list[Approval]:
    """Read-only visibility, intentionally NOT scoped by reviewer/manager
    relationship — this is a small-team ops dashboard, not a multi-tenant
    system, and anyone with the shared API key can see every approval
    regardless of who it targets. The real authorization boundary is who
    may DECIDE an approval, which IS scoped (require_reviewer_token +
    app/api/rbac.py's manager-relationship check on
    POST /approvals/{id}/decide). If this app grows into something where
    read-visibility itself needs to be restricted per-reviewer, that would
    be a deliberate follow-up, not an oversight being silently left here.
    """
    async with session_scope() as session:
        query = select(Approval).order_by(Approval.created_at.desc())
        if status:
            try:
                query = query.where(Approval.status == ApprovalStatus(status))
            except ValueError:
                raise HTTPException(400, f"Invalid status: {status!r}")
        rows = await session.scalars(query)
        return list(rows)


@app.post(
    "/approvals/{approval_id}/decide",
    response_model=RunResult,
    dependencies=[Depends(require_api_key)],
)
@limiter.limit("20/minute")
async def decide_approval(
    request: Request,
    approval_id: int,
    payload: ApprovalDecision,
    reviewer: Reviewer = Depends(require_reviewer_token),
) -> RunResult:
    """Human reviewer approves or rejects a pending sensitive action, then the
    agent graph is resumed from exactly where it paused.

    Authorization (Stage 4.2, scoped down): `reviewer` is resolved from the
    caller's X-Reviewer-Token (require_reviewer_token), never from a
    request-body field — that's what actually binds this decision to a
    specific person rather than a self-asserted name anyone holding the
    shared API key could type in. From there, an it_admin reviewer may
    decide any sensitive approval; a manager reviewer may only decide
    approvals targeting their own direct reports (app/api/rbac.py).
    """
    async with session_scope() as session:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(404, f"No such approval: {approval_id}")
        if approval.status != ApprovalStatus.PENDING:
            raise HTTPException(409, f"Approval {approval_id} already {approval.status.value}")

        try:
            await authorize_reviewer(session, reviewer.username, approval)
        except ApprovalNotAuthorizedError as exc:
            raise HTTPException(403, str(exc)) from exc

        from datetime import datetime, timezone

        approval.status = ApprovalStatus.APPROVED if payload.approve else ApprovalStatus.REJECTED
        approval.reviewer = reviewer.username
        approval.resolved_at = datetime.now(timezone.utc)
        ticket_id = approval.ticket_id

        if not payload.approve:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is not None:
                ticket.status = TicketStatus.REJECTED
                ticket.result_summary = (
                    f"Sensitive action {approval.tool_name} rejected by {reviewer.username}."
                )

    if not payload.approve:
        return RunResult(
            ticket_id=ticket_id, done=True, plan=[], results=[],
            error="Rejected by reviewer", interrupted=False, pending_approval=None,
        )

    result = await resume_ticket_run(ticket_id)
    return RunResult(**result)


@app.post(
    "/approvals/{approval_id}/decide/stream",
    dependencies=[Depends(require_api_key)],
)
@limiter.limit("20/minute")
async def decide_approval_stream(
    request: Request,
    approval_id: int,
    payload: ApprovalDecision,
    reviewer: Reviewer = Depends(require_reviewer_token),
) -> StreamingResponse:
    """AG-UI-protocol streaming counterpart to POST /approvals/{id}/decide:
    same authorization and decision recording, but a resumed (approved) run
    streams its remaining STEP_*/TOOL_CALL_*/RUN_FINISHED events over SSE
    instead of blocking until the run's next interrupt or completion.
    """
    async with session_scope() as session:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(404, f"No such approval: {approval_id}")
        if approval.status != ApprovalStatus.PENDING:
            raise HTTPException(409, f"Approval {approval_id} already {approval.status.value}")

        try:
            await authorize_reviewer(session, reviewer.username, approval)
        except ApprovalNotAuthorizedError as exc:
            raise HTTPException(403, str(exc)) from exc

        from datetime import datetime, timezone

        approval.status = ApprovalStatus.APPROVED if payload.approve else ApprovalStatus.REJECTED
        approval.reviewer = reviewer.username
        approval.resolved_at = datetime.now(timezone.utc)
        ticket_id = approval.ticket_id

        if not payload.approve:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is not None:
                ticket.status = TicketStatus.REJECTED
                ticket.result_summary = (
                    f"Sensitive action {approval.tool_name} rejected by {reviewer.username}."
                )

    encoder = EventEncoder()
    run_id = str(uuid.uuid4())

    if not payload.approve:
        from ag_ui.core import RunFinishedEvent, RunFinishedSuccessOutcome, RunStartedEvent

        async def rejection_source():
            yield encoder.encode(RunStartedEvent(thread_id=f"ticket-{ticket_id}", run_id=run_id))
            yield encoder.encode(
                RunFinishedEvent(
                    thread_id=f"ticket-{ticket_id}",
                    run_id=run_id,
                    outcome=RunFinishedSuccessOutcome(),
                    result={"error": "Rejected by reviewer", "done": True},
                )
            )

        return StreamingResponse(rejection_source(), media_type=encoder.get_content_type())

    async def event_source():
        async for event in stream_resume_run(ticket_id, run_id):
            yield encoder.encode(event)

    return StreamingResponse(event_source(), media_type=encoder.get_content_type())


@app.get(
    "/tickets/{ticket_id}/audit",
    response_model=list[AuditLogOut],
    dependencies=[Depends(require_api_key)],
)
async def get_ticket_audit(ticket_id: int) -> list[AuditLog]:
    """Read-only, intentionally NOT scoped by reviewer relationship — see
    list_approvals' docstring above for why: this is a small-team ops
    dashboard's audit trail, not a per-tenant restricted view, and the real
    authorization boundary (who may DECIDE a sensitive action) is enforced
    elsewhere.
    """
    async with session_scope() as session:
        rows = await session.scalars(
            select(AuditLog).where(AuditLog.ticket_id == ticket_id).order_by(AuditLog.created_at)
        )
        return list(rows)


@app.post(
    "/admin/sla-sweep",
    response_model=SlaSweepResult,
    dependencies=[Depends(require_api_key)],
)
async def trigger_sla_sweep() -> SlaSweepResult:
    """Runs one SLA sweep pass on demand — the same logic the background
    loop runs every `SLA_SWEEP_INTERVAL_SECONDS`, exposed here for ops
    visibility/testing without waiting for the next scheduled pass.
    """
    result = await run_sla_sweep()
    return SlaSweepResult(**result)
