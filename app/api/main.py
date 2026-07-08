import asyncio
import contextlib
import datetime as dt
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
from app.agent.demo_purge import demo_purge_loop, reset_demo_data_if_due
from app.agent.runner import resume_ticket_run, start_ticket_run
from app.agent.sla_sweep import run_sla_sweep, sla_sweep_loop
from app.api.auth import require_api_client, require_reviewer_token
from app.api.idempotency import get_cached_response, store_response
from app.api.rbac import ApprovalNotAuthorizedError, authorize_reviewer
from app.api.schemas import (
    ApprovalDecision,
    ApprovalOut,
    AuditLogOut,
    DemoResetResult,
    EmployeeOut,
    RunResult,
    SlaSweepResult,
    TicketCreate,
    TicketOut,
)
from app.config import get_settings
from app.db.models import (
    ApiClient,
    ApiClientRole,
    Approval,
    ApprovalStatus,
    AuditLog,
    EmployeeUser,
    Reviewer,
    ReviewerRole,
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


async def _ensure_bootstrap_admin_client() -> None:
    """Ensures a real ApiClient row exists with `key == settings.api_key`,
    so an existing deployment's X-API-Key keeps working unchanged after the
    migration off the old bare-string-compare design — an admin client is
    created automatically here rather than requiring db/seed.py to be run
    manually before the app can serve its first authenticated request.
    """
    api_key = get_settings().api_key
    if not api_key:
        return
    async with session_scope() as session:
        existing = await session.scalar(select(ApiClient).where(ApiClient.key == api_key))
        if existing is None:
            session.add(ApiClient(name="bootstrap-admin", role=ApiClientRole.ADMIN, key=api_key))


# A stranger trying the public demo has no way to obtain a real API key —
# GET /demo-key (below) hands this one out on request, deliberately public.
# A low daily cap keeps it from being a meaningful cost/abuse vector even
# though the key itself is effectively public.
DEMO_CLIENT_DAILY_REQUEST_LIMIT = 10


async def _ensure_demo_guest_client() -> None:
    """Ensures a real, low-privilege ApiClient row exists with
    `key == settings.demo_api_key`, if configured — see Settings.demo_api_key
    for why this is opt-in. STANDARD role: same ticket/audit/approval
    read-scoping as any other non-admin client (only sees tickets it itself
    filed), plus a much lower daily_request_limit than the default 100,
    since this key is served to literally anyone who asks.
    """
    demo_key = get_settings().demo_api_key
    if not demo_key:
        return
    async with session_scope() as session:
        existing = await session.scalar(select(ApiClient).where(ApiClient.key == demo_key))
        if existing is None:
            session.add(
                ApiClient(
                    name="public-demo-guest",
                    role=ApiClientRole.STANDARD,
                    key=demo_key,
                    daily_request_limit=DEMO_CLIENT_DAILY_REQUEST_LIMIT,
                )
            )


DEMO_REVIEWER_USERNAME = "public-demo-reviewer"


async def _ensure_demo_reviewer() -> None:
    """Ensures a Reviewer row named DEMO_REVIEWER_USERNAME exists, if
    DEMO_API_KEY is configured, so a public demo visitor's HITL approvals
    are fully self-contained — without this, the ONLY way to approve a
    sensitive action on a demo-submitted ticket would be a real reviewer
    token (mchen/admin from app/db/seed.py), which would mean either handing
    out a real reviewer's credential publicly (defeats the point of a
    low-privilege demo key) or leaving every demo ticket's sensitive step
    permanently stuck pending.

    role=IT_ADMIN (so app/api/rbac.py's authorize_reviewer lets it decide
    ANY approval by role) is intentionally paired with a SEPARATE, stricter
    check — decide_approval in this module additionally requires the
    approval's ticket to be owned by the demo ApiClient before this specific
    reviewer may decide it. IT_ADMIN alone would otherwise let a public demo
    visitor decide real, non-demo approvals; the extra ownership check is
    what actually confines it to demo-owned tickets. See decide_approval.

    Token is generated once (like any Reviewer.token — see
    _default_reviewer_token) and persisted, not regenerated on every
    restart, so GET /demo-key keeps returning a working token across
    redeploys.
    """
    if not get_settings().demo_api_key:
        return
    async with session_scope() as session:
        existing = await session.scalar(select(Reviewer).where(Reviewer.username == DEMO_REVIEWER_USERNAME))
        if existing is None:
            session.add(Reviewer(username=DEMO_REVIEWER_USERNAME, role=ReviewerRole.IT_ADMIN))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not get_settings().api_key:
        logger.warning(
            "API_KEY is not set — /tickets, /approvals, and /audit endpoints are "
            "UNAUTHENTICATED. Set API_KEY in .env before exposing this beyond localhost."
        )
    await init_db()
    await _ensure_bootstrap_admin_client()
    await _ensure_demo_guest_client()
    await _ensure_demo_reviewer()
    sla_sweep_task = asyncio.create_task(sla_sweep_loop())
    demo_purge_task = asyncio.create_task(demo_purge_loop())
    yield
    sla_sweep_task.cancel()
    demo_purge_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sla_sweep_task
    with contextlib.suppress(asyncio.CancelledError):
        await demo_purge_task
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


@app.api_route("/health", methods=["GET", "HEAD"])
async def health() -> dict:
    # Explicit methods=["GET", "HEAD"], not the bare @app.get shorthand:
    # FastAPI 0.139.0 does not auto-add HEAD support the way Starlette's own
    # Route claims to (confirmed live — uptime monitors like shields.io and
    # Better Stack, both documented in DEPLOYMENT.md, issue HEAD requests
    # against /health by default, and every one of them got a 405 here,
    # rendering this app as permanently "down" on their dashboards/badges
    # despite GET /health responding 200 the entire time). Reproduced with a
    # minimal two-line FastAPI app on this exact pinned version — an
    # upstream framework quirk, not anything specific to this route.
    from app.mcp_server.circuit_breaker import snapshot_all_breakers

    return {"status": "ok", "mcp_domains": snapshot_all_breakers()}


@app.api_route("/ready", methods=["GET", "HEAD"])
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
    """Intentionally NOT behind require_api_client: this serves only the
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


@app.get("/demo-key")
async def demo_key() -> dict:
    """Deliberately unauthenticated — hands out DEMO_API_KEY (if configured)
    plus the seeded demo reviewer's token (see _ensure_demo_reviewer), so a
    stranger trying the public dashboard doesn't need you to give them a
    real credential for either header — without the reviewer token, a demo
    visitor could submit tickets but could never actually approve/reject
    the sensitive-action step a demo onboarding/offboarding ticket pauses
    on, leaving every demo ticket stuck. Returns both as null if
    unconfigured; the frontend then falls back to its normal
    "type your own key/token" behavior.
    """
    settings = get_settings()
    reviewer_token: str | None = None
    if settings.demo_api_key:
        async with session_scope() as session:
            reviewer = await session.scalar(
                select(Reviewer).where(Reviewer.username == DEMO_REVIEWER_USERNAME)
            )
            reviewer_token = reviewer.token if reviewer is not None else None
    return {"api_key": settings.demo_api_key or None, "reviewer_token": reviewer_token}


async def _check_and_increment_daily_request_count(client: ApiClient | None) -> None:
    """Bounds sustained LLM cost per caller — a security review found that
    with only a per-minute rate limit, one ApiClient could still sustain
    substantial ongoing LLM spend indefinitely (max-length ticket bodies,
    repeated in a loop, at the per-minute cap, forever). This is a request-
    COUNT budget, not true token accounting: attributing actual token spend
    to a specific caller would require threading ApiClient identity through
    AgentState/the LangGraph checkpointer (resumed across HITL pauses,
    potentially hours later) and every LLM call site in app/agent/graph.py —
    a substantially larger change than this. A daily request cap bounds the
    same abuse pattern without that plumbing.

    No-op if client is None (API_KEY unset — local demo mode already trusts
    everyone; nothing to attribute a per-caller budget to).
    """
    if client is None:
        return
    now = dt.datetime.now(dt.timezone.utc)
    async with session_scope() as session:
        row = await session.get(ApiClient, client.id)
        if row is None:
            return
        # SQLite (via aiosqlite) doesn't reliably round-trip tzinfo on
        # DateTime(timezone=True) columns the way Postgres does — a value
        # written as timezone-aware can come back naive, which raises
        # TypeError on subtraction against `now`. Assume UTC (everything
        # this app writes to this column already is) rather than compare
        # inside a SQL WHERE clause the way sla_sweep.py does, since this
        # check is a single-row lookup by primary key, not a query.
        reset_at = row.request_count_reset_at
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=dt.timezone.utc)
        if now - reset_at > dt.timedelta(days=1):
            row.daily_request_count = 0
            row.request_count_reset_at = now
        if row.daily_request_count >= row.daily_request_limit:
            raise HTTPException(
                429,
                f"Daily request limit ({row.daily_request_limit}/day) reached for this API client. "
                "Try again after the daily reset.",
            )
        row.daily_request_count += 1


@app.post("/tickets", response_model=RunResult)
@limiter.limit("20/minute")
async def submit_ticket(
    request: Request,
    payload: TicketCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    client: ApiClient | None = Depends(require_api_client),
) -> RunResult:
    """Creates a ticket and immediately runs the agent on it up to the first
    HITL interrupt (or completion, if no sensitive actions are needed).

    If an Idempotency-Key header is supplied and was already used with this
    exact request body, replays the stored response instead of creating a
    duplicate ticket and re-running the agent graph.
    """
    await _check_and_increment_daily_request_count(client)
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
            submitted_by_client_id=client.id if client is not None else None,
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


@app.post("/tickets/stream")
@limiter.limit("20/minute")
async def submit_ticket_stream(
    request: Request, payload: TicketCreate, client: ApiClient | None = Depends(require_api_client)
) -> StreamingResponse:
    """AG-UI-protocol streaming counterpart to POST /tickets (see
    app/agent/ag_ui_bridge.py): creates the ticket the same way, then
    streams RUN_STARTED/STEP_*/TOOL_CALL_*/STATE_DELTA/RUN_FINISHED events
    over SSE as the graph actually executes, instead of blocking until the
    first interrupt or completion and returning one JSON blob.

    No Idempotency-Key support here — replaying a cached *stream* doesn't
    make sense the way replaying a cached final JSON result does; a client
    that needs idempotent ticket submission should use POST /tickets.
    """
    await _check_and_increment_daily_request_count(client)
    async with session_scope() as session:
        ticket = Ticket(
            requester=payload.requester,
            subject=payload.subject,
            body=payload.body,
            status=TicketStatus.PLANNING,
            submitted_by_client_id=client.id if client is not None else None,
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


def _client_may_see_ticket(client: ApiClient | None, ticket: Ticket) -> bool:
    """Whether client may read this specific ticket (and, by extension, its
    approvals/audit entries). None (API_KEY unset, local demo mode) and
    ADMIN clients see everything, matching this app's pre-existing
    behavior for a small-team ops setup. A STANDARD client may only see
    tickets it itself submitted — closes a security-review finding: the
    single shared API key previously meant "may submit tickets" was
    indistinguishable from "may read every employee's audit trail/access
    history company-wide," since these reads had no caller-scoping at all.

    Scoped by Ticket.submitted_by_client_id (who actually authenticated the
    POST /tickets call), NOT Ticket.requester — requester is free-text the
    caller puts in the request BODY, entirely decoupled from which
    credential made the call. An earlier version of this compared
    `ticket.requester == client.name`, which broke both directions on a
    live public demo: a caller submitting with a `requester` value that
    didn't happen to equal their own ApiClient's `name` (e.g. the demo
    client, literally named "public-demo-guest") couldn't see their OWN
    just-submitted ticket, and nothing stopped a DIFFERENT caller from
    seeing someone else's ticket by guessing/matching the right requester
    string instead. See Ticket.submitted_by_client_id's docstring.
    """
    if client is None or client.role == ApiClientRole.ADMIN:
        return True
    return ticket.submitted_by_client_id == client.id


async def _demo_client_id(session) -> int | None:
    """The public demo ApiClient's id, if DEMO_API_KEY is configured —
    used to keep its own tickets/approvals out of ADMIN's default view
    (see list_tickets/list_approvals' include_demo param) so the ops
    dashboard isn't cluttered with public demo traffic day to day, on top
    of the daily hard-delete app/agent/demo_purge.py already does. Cheap to
    just query every time (one indexed lookup) rather than caching — this
    project has no cache-invalidation story and demo config essentially
    never changes at runtime.
    """
    demo_key = get_settings().demo_api_key
    if not demo_key:
        return None
    demo_client = await session.scalar(select(ApiClient).where(ApiClient.key == demo_key))
    return demo_client.id if demo_client is not None else None


@app.get("/tickets", response_model=list[TicketOut])
async def list_tickets(
    include_demo: bool = False, client: ApiClient | None = Depends(require_api_client)
) -> list[Ticket]:
    """`include_demo=true` reveals the public demo client's own tickets in
    an ADMIN's view (hidden by default — see _demo_client_id) — for a
    STANDARD client this param has no effect, since it can only ever see
    its own tickets regardless."""
    async with session_scope() as session:
        query = select(Ticket).order_by(Ticket.created_at.desc())
        if client is not None and client.role != ApiClientRole.ADMIN:
            query = query.where(Ticket.submitted_by_client_id == client.id)
        elif not include_demo:
            demo_id = await _demo_client_id(session)
            if demo_id is not None:
                # IS DISTINCT FROM, not !=: plain != excludes NULL rows too
                # under standard SQL three-valued logic (NULL != x is NULL,
                # not TRUE) — pre-existing tickets from before
                # submitted_by_client_id existed (or any future row with no
                # attributable client) have NULL here and must still show
                # up in the default admin view; only the demo client's OWN
                # rows should be hidden.
                query = query.where(Ticket.submitted_by_client_id.is_distinct_from(demo_id))
        rows = await session.scalars(query)
        return list(rows)


@app.get("/tickets/{ticket_id}", response_model=TicketOut)
async def get_ticket(ticket_id: int, client: ApiClient | None = Depends(require_api_client)) -> Ticket:
    async with session_scope() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            raise HTTPException(404, f"No such ticket: {ticket_id}")
        if not _client_may_see_ticket(client, ticket):
            raise HTTPException(404, f"No such ticket: {ticket_id}")
        return ticket


@app.get("/employees", response_model=list[EmployeeOut])
async def list_employees(
    status: str | None = None,
    include_demo: bool = False,
    client: ApiClient | None = Depends(require_api_client),
) -> list[EmployeeUser]:
    """Current (active) and past (disabled) employees in the mock identity store.

    Previously had NO caller-scoping at all (dependencies=[Depends(require_api_client)]
    only checked that SOME valid key was presented) — any authenticated
    caller, including the low-privilege public DEMO_API_KEY, could read
    every real employee's full name/email/department/access grants. Found
    live: a demo-key visitor saw the actual company directory. Fixed the
    same way as list_tickets/list_approvals: a STANDARD client only sees
    employees IT created (EmployeeUser.owned_by_client_id, set by
    identity_create_user — see app/mcp_server/tools.py's create_user).
    ADMIN (and API_KEY-unset local demo mode) still sees everyone by
    default, EXCEPT the public demo client's own employees, hidden the same
    way as list_tickets/list_approvals' include_demo param (pass
    include_demo=true to reveal them) — found live: a demo visitor's
    onboarding-created employee (e.g. a fictional "tuser") showed up
    unfiltered in the admin's own employee directory, inconsistent with how
    demo tickets/approvals are already hidden by default there.
    """
    async with session_scope() as session:
        query = select(EmployeeUser).order_by(EmployeeUser.full_name)
        if status:
            try:
                query = query.where(EmployeeUser.status == UserStatus(status))
            except ValueError:
                raise HTTPException(400, f"Invalid status: {status!r}")
        if client is not None and client.role != ApiClientRole.ADMIN:
            query = query.where(EmployeeUser.owned_by_client_id == client.id)
        elif not include_demo:
            demo_id = await _demo_client_id(session)
            if demo_id is not None:
                # IS DISTINCT FROM — see list_tickets's identical comment
                # for why plain != would incorrectly hide NULL-owned
                # (pre-existing/unattributed) employee rows too.
                query = query.where(EmployeeUser.owned_by_client_id.is_distinct_from(demo_id))
        rows = await session.scalars(query)
        return list(rows)


@app.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    status: str | None = None,
    include_demo: bool = False,
    client: ApiClient | None = Depends(require_api_client),
) -> list[Approval]:
    """Read-only visibility, scoped to STANDARD clients' own tickets — see
    _client_may_see_ticket. ADMIN clients (and API_KEY-unset local demo
    mode) still see every OTHER approval regardless of who it targets, as
    before for a small-team ops dashboard use case — except the public demo
    client's own approvals, hidden by default the same way as
    list_tickets's include_demo (pass include_demo=true to reveal them).
    The real authorization boundary for DECIDING an approval was already
    scoped separately (require_reviewer_token + app/api/rbac.py's manager-
    relationship check on POST /approvals/{id}/decide) — this only closes
    the READ side.
    """
    async with session_scope() as session:
        query = select(Approval).order_by(Approval.created_at.desc())
        if status:
            try:
                query = query.where(Approval.status == ApprovalStatus(status))
            except ValueError:
                raise HTTPException(400, f"Invalid status: {status!r}")
        if client is not None and client.role != ApiClientRole.ADMIN:
            query = query.join(Ticket, Approval.ticket_id == Ticket.id).where(
                Ticket.submitted_by_client_id == client.id
            )
        elif not include_demo:
            demo_id = await _demo_client_id(session)
            if demo_id is not None:
                # IS DISTINCT FROM — see list_tickets's identical comment
                # for why plain != would incorrectly hide NULL-owned rows.
                query = query.join(Ticket, Approval.ticket_id == Ticket.id).where(
                    Ticket.submitted_by_client_id.is_distinct_from(demo_id)
                )
        rows = await session.scalars(query)
        return list(rows)


async def _authorize_demo_reviewer_scope(session, reviewer: Reviewer, approval: Approval) -> None:
    """Additional restriction on top of authorize_reviewer, for the seeded
    public demo reviewer ONLY (see _ensure_demo_reviewer): that reviewer is
    role=IT_ADMIN so app/api/rbac.py's own rule would otherwise let it decide
    ANY approval, real or demo. This closes that gap — the demo reviewer may
    only decide approvals whose ticket is owned by the demo ApiClient, so a
    public demo visitor holding this publicly-served token can never
    approve/reject a real, non-demo sensitive action.

    A no-op for every other reviewer (mchen/admin/anyone from db/seed.py) —
    those keep whatever app/api/rbac.py's role-based rule already grants.
    """
    if reviewer.username != DEMO_REVIEWER_USERNAME:
        return
    demo_id = await _demo_client_id(session)
    ticket = await session.get(Ticket, approval.ticket_id)
    if demo_id is None or ticket is None or ticket.submitted_by_client_id != demo_id:
        raise ApprovalNotAuthorizedError(
            f"{reviewer.username!r} may only decide approvals on tickets submitted "
            "via the public demo API key — not approval "
            f"{approval.id}."
        )


@app.post(
    "/approvals/{approval_id}/decide",
    response_model=RunResult,
    dependencies=[Depends(require_api_client)],
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
    approvals targeting their own direct reports (app/api/rbac.py) — except
    the seeded public demo reviewer, further confined to demo-owned tickets
    only (see _authorize_demo_reviewer_scope).
    """
    async with session_scope() as session:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(404, f"No such approval: {approval_id}")
        if approval.status != ApprovalStatus.PENDING:
            raise HTTPException(409, f"Approval {approval_id} already {approval.status.value}")

        try:
            await authorize_reviewer(session, reviewer.username, approval)
            await _authorize_demo_reviewer_scope(session, reviewer, approval)
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
    dependencies=[Depends(require_api_client)],
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
            await _authorize_demo_reviewer_scope(session, reviewer, approval)
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


@app.get("/tickets/{ticket_id}/audit", response_model=list[AuditLogOut])
async def get_ticket_audit(
    ticket_id: int, client: ApiClient | None = Depends(require_api_client)
) -> list[AuditLog]:
    """Scoped to STANDARD clients' own tickets — see _client_may_see_ticket.
    ADMIN clients (and API_KEY-unset local demo mode) still see every
    ticket's audit trail, as before. Previously this had no caller-scoping
    at all — a security review flagged that failure text and raw tool_args
    (target usernames, resource names) here were readable by any API-key
    holder for any ticket, not just the one they filed themselves.
    """
    async with session_scope() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None or not _client_may_see_ticket(client, ticket):
            raise HTTPException(404, f"No such ticket: {ticket_id}")
        rows = await session.scalars(
            select(AuditLog).where(AuditLog.ticket_id == ticket_id).order_by(AuditLog.created_at)
        )
        return list(rows)


@app.post(
    "/admin/sla-sweep",
    response_model=SlaSweepResult,
    dependencies=[Depends(require_api_client)],
)
@limiter.limit("10/minute")
async def trigger_sla_sweep(request: Request) -> SlaSweepResult:
    """Runs one SLA sweep pass on demand — the same logic the background
    loop runs every `SLA_SWEEP_INTERVAL_SECONDS`, exposed here for ops
    visibility/testing without waiting for the next scheduled pass.

    Rate-limited (unlike being left unbounded) because each call runs three
    full-table scans (overdue approvals, stuck tickets, expired idempotency
    keys) and writes a fresh audit-log row for every already-overdue item it
    finds again — an unthrottled loop against this endpoint would drive
    sustained DB load and unbounded audit-table growth for no operational
    benefit, since the background loop already covers routine sweeping.
    """
    result = await run_sla_sweep()
    return SlaSweepResult(**result)


@app.post("/admin/demo-reset", response_model=DemoResetResult)
@limiter.limit("10/minute")
async def trigger_demo_reset(
    request: Request, client: ApiClient | None = Depends(require_api_client)
) -> DemoResetResult:
    """Runs one demo-data-reset pass on demand — the same check the
    background loop runs every hour (app/agent/demo_purge.py), exposed here
    for ops visibility/testing without waiting for it to become due on its
    own. A no-op (0 purged) if DEMO_API_KEY is unset, or if the reset
    interval hasn't elapsed yet since the last purge.

    Unlike /admin/sla-sweep (any authenticated client may trigger — it only
    escalates/flags, never deletes), this genuinely restricts to ADMIN:
    this endpoint hard-deletes data, so the bar for who may trigger it is
    deliberately higher than "holds some valid API key."
    """
    if client is None or client.role != ApiClientRole.ADMIN:
        raise HTTPException(403, "Only an admin API client may trigger a demo data reset.")
    purged = await reset_demo_data_if_due()
    return DemoResetResult(tickets_purged=purged)
