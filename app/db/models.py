import enum
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_sla_deadline() -> datetime:
    """Fallback used when an Approval is constructed without an explicit
    sla_deadline (e.g. most existing tests, which predate Stage 4.5 and
    don't care about SLA behavior). app.agent.graph.await_approval_node
    always passes an explicit deadline derived from
    Settings.approval_sla_minutes for real approval rows — this default
    only exists so the column can be NOT NULL without every call site
    needing to know about SLAs.
    """
    return utcnow() + timedelta(hours=1)


def _default_reviewer_token() -> str:
    """Fallback used when a Reviewer is constructed without an explicit
    token (e.g. tests that only exercise app.api.rbac's authorization
    logic directly with a username, never going through
    require_reviewer_token's actual authentication). app/db/seed.py always
    generates and prints a real per-reviewer token for actual use — this
    default only exists so the column can be NOT NULL/unique without every
    call site needing to supply one.
    """
    return secrets.token_urlsafe(24)


class Base(DeclarativeBase):
    pass


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class EmployeeUser(Base):
    """Mock enterprise identity record — stands in for IBM ID Management / AD / Okta."""

    __tablename__ = "employee_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(128))
    department: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus), default=UserStatus.ACTIVE
    )
    access_grants: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Lightweight RBAC (Stage 4.2, scoped down — no real IdP/OIDC): the
    # username of this employee's manager, used to decide who besides an
    # it_admin reviewer may approve a sensitive action targeting them.
    manager_username: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ReviewerRole(str, enum.Enum):
    IT_ADMIN = "it_admin"
    MANAGER = "manager"


class Reviewer(Base):
    """Lightweight stand-in for a real OIDC-verified reviewer identity
    (Stage 4.1/4.2, scoped down — see ROADMAP.md's Stage 4 trap notes: a
    real Keycloak/OIDC deployment was explicitly out of scope for this
    project).

    `token` is a per-reviewer secret (see app/api/auth.py's
    require_reviewer_token) presented via the X-Reviewer-Token header —
    this is what actually binds a request to a specific reviewer identity.
    Without it, `username` alone would be a self-asserted claim: anyone
    holding the one shared API key could type any registered reviewer's
    name into the request body and approve/reject on their behalf. The
    token closes that gap; `username` still drives the role/manager-
    relationship authorization logic in app/api/rbac.py, but only after the
    token has proven the caller actually is that reviewer.
    """

    __tablename__ = "reviewers"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[ReviewerRole] = mapped_column(Enum(ReviewerRole), default=ReviewerRole.MANAGER)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=_default_reviewer_token)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TicketStatus(str, enum.Enum):
    RECEIVED = "received"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class Ticket(Base):
    """An incoming onboarding/offboarding/troubleshooting request."""

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    requester: Mapped[str] = mapped_column(String(128))
    subject: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus), default=TicketStatus.RECEIVED
    )
    result_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan"
    )
    audit_entries: Mapped[list["AuditLog"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan"
    )


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class Approval(Base):
    """Human-in-the-loop gate for a sensitive tool call proposed by the agent."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    tool_name: Mapped[str] = mapped_column(String(64))
    tool_args: Mapped[dict] = mapped_column(JSON, default=dict)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus), default=ApprovalStatus.PENDING
    )
    reviewer: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Stage 4.5 (scoped down): a fixed SLA window from creation, past which
    # the background sweep (app/agent/sla_sweep.py) escalates a still-PENDING
    # approval instead of leaving it silently stuck forever.
    sla_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_default_sla_deadline)
    escalated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set the first (and only) time approval_gate.require_approval() lets a
    # sensitive tool call through for this approval — status stays APPROVED
    # (so audit/display logic doesn't need a new terminal state), but a
    # second attempt to use the SAME approval_id is refused once this is
    # set. Without this, one human sign-off would authorize the sensitive
    # action an unlimited number of times: nothing else in this schema
    # marks an Approval "already used."
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    ticket: Mapped["Ticket"] = relationship(back_populates="approvals")


class IdempotencyKey(Base):
    """Dedup record for POST /tickets — a client-supplied Idempotency-Key
    resubmitted with the same request body replays the stored response
    instead of re-running the agent graph and creating a duplicate ticket.
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Immutable record of every tool invocation the MCP server executed."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    actor: Mapped[str] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(64))
    tool_args: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    ticket: Mapped["Ticket | None"] = relationship(back_populates="audit_entries")
