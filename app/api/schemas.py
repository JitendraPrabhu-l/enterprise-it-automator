from datetime import datetime

from pydantic import BaseModel, Field


class TicketCreate(BaseModel):
    # Bounds match the Ticket DB columns' declared sizes (String(128)/
    # String(256)) — SQLite doesn't enforce a VARCHAR length itself, so
    # without this an oversized value would silently succeed at the DB
    # layer while violating the schema's own stated intent. `body` has no
    # DB-level cap (Text column) but gets one here regardless: an unbounded
    # body is embedded directly into the LLM planner prompt (app/agent/graph.py),
    # so without a cap a client could submit a multi-megabyte ticket,
    # inflating LLM token cost/spend on every planning call for that ticket.
    requester: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=20_000)


class TicketOut(BaseModel):
    id: int
    requester: str
    subject: str
    body: str
    status: str
    result_summary: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EmployeeOut(BaseModel):
    id: int
    username: str
    full_name: str
    email: str
    department: str
    status: str
    access_grants: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ApprovalOut(BaseModel):
    id: int
    ticket_id: int
    tool_name: str
    tool_args: dict
    reasoning: str
    status: str
    reviewer: str
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class ApprovalDecision(BaseModel):
    # No `reviewer` field: who is deciding comes from the authenticated
    # X-Reviewer-Token (see app/api/auth.py's require_reviewer_token), never
    # from a client-supplied value — a request body field here would just
    # be a self-asserted claim an attacker could set to any name.
    approve: bool


class AuditLogOut(BaseModel):
    id: int
    ticket_id: int | None
    actor: str
    tool_name: str
    tool_args: dict
    result: str
    success: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class RunResult(BaseModel):
    ticket_id: int
    done: bool
    plan: list[dict]
    results: list[dict]
    error: str | None
    interrupted: bool
    pending_approval: dict | None = None


class SlaSweepResult(BaseModel):
    escalated_approvals: list[int]
    stuck_tickets: list[int]


class DemoResetResult(BaseModel):
    tickets_purged: int
