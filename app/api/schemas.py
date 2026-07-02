from datetime import datetime

from pydantic import BaseModel


class TicketCreate(BaseModel):
    requester: str
    subject: str
    body: str


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
    reviewer: str
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
