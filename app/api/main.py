import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.agent.runner import resume_ticket_run, start_ticket_run
from app.api.schemas import (
    ApprovalDecision,
    ApprovalOut,
    AuditLogOut,
    RunResult,
    TicketCreate,
    TicketOut,
)
from app.db.models import Approval, ApprovalStatus, AuditLog, Ticket, TicketStatus
from app.db.session import init_db, session_scope

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="MCP-Enabled Enterprise IT Automator",
    description=(
        "LangGraph agent that automates employee onboarding/offboarding via a "
        "custom MCP server, with human-in-the-loop approval for sensitive actions."
    ),
    lifespan=lifespan,
)


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/tickets", response_model=RunResult)
async def submit_ticket(payload: TicketCreate) -> RunResult:
    """Creates a ticket and immediately runs the agent on it up to the first
    HITL interrupt (or completion, if no sensitive actions are needed)."""
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
    return RunResult(**result)


@app.get("/tickets", response_model=list[TicketOut])
async def list_tickets() -> list[Ticket]:
    async with session_scope() as session:
        rows = await session.scalars(select(Ticket).order_by(Ticket.created_at.desc()))
        return list(rows)


@app.get("/tickets/{ticket_id}", response_model=TicketOut)
async def get_ticket(ticket_id: int) -> Ticket:
    async with session_scope() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            raise HTTPException(404, f"No such ticket: {ticket_id}")
        return ticket


@app.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(status: str | None = None) -> list[Approval]:
    async with session_scope() as session:
        query = select(Approval).order_by(Approval.created_at.desc())
        if status:
            try:
                query = query.where(Approval.status == ApprovalStatus(status))
            except ValueError:
                raise HTTPException(400, f"Invalid status: {status!r}")
        rows = await session.scalars(query)
        return list(rows)


@app.post("/approvals/{approval_id}/decide", response_model=RunResult)
async def decide_approval(approval_id: int, payload: ApprovalDecision) -> RunResult:
    """Human reviewer approves or rejects a pending sensitive action, then the
    agent graph is resumed from exactly where it paused."""
    async with session_scope() as session:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(404, f"No such approval: {approval_id}")
        if approval.status != ApprovalStatus.PENDING:
            raise HTTPException(409, f"Approval {approval_id} already {approval.status.value}")

        from datetime import datetime, timezone

        approval.status = ApprovalStatus.APPROVED if payload.approve else ApprovalStatus.REJECTED
        approval.reviewer = payload.reviewer
        approval.resolved_at = datetime.now(timezone.utc)
        ticket_id = approval.ticket_id

        if not payload.approve:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is not None:
                ticket.status = TicketStatus.REJECTED
                ticket.result_summary = (
                    f"Sensitive action {approval.tool_name} rejected by {payload.reviewer}."
                )

    if not payload.approve:
        return RunResult(
            ticket_id=ticket_id, done=True, plan=[], results=[],
            error="Rejected by reviewer", interrupted=False, pending_approval=None,
        )

    result = await resume_ticket_run(ticket_id)
    return RunResult(**result)


@app.get("/tickets/{ticket_id}/audit", response_model=list[AuditLogOut])
async def get_ticket_audit(ticket_id: int) -> list[AuditLog]:
    async with session_scope() as session:
        rows = await session.scalars(
            select(AuditLog).where(AuditLog.ticket_id == ticket_id).order_by(AuditLog.created_at)
        )
        return list(rows)
