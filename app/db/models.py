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


def _default_api_client_key() -> str:
    """Same rationale as _default_reviewer_token, for ApiClient.key."""
    return secrets.token_urlsafe(32)


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
    # Set only for employees created via identity_create_user while running a
    # ticket submitted by a non-ADMIN ApiClient (app/mcp_server/tools.py's
    # create_user, threaded from Ticket.submitted_by_client_id) — NULL for
    # every employee seeded directly (app/db/seed.py) or created under the
    # unauthenticated/ADMIN API_KEY. This is what lets GET /employees hide
    # real company employees from a STANDARD/demo caller while still
    # showing that caller the fictional employees it created itself: same
    # "who actually owns this row" pattern as Ticket.submitted_by_client_id,
    # applied to the OTHER piece of state a demo ticket can create. Also
    # used by app/agent/demo_purge.py to hard-delete these rows alongside
    # the demo client's tickets/approvals/audit entries.
    owned_by_client_id: Mapped[int | None] = mapped_column(ForeignKey("api_clients.id"), nullable=True)
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
    # Set once, when this reviewer links their Telegram account by sending
    # `/start <their own token>` to the bot (see app/notifications/telegram.py) —
    # never entered by a user into any web form, so a chat_id alone can
    # never be replayed against the wrong reviewer: the bot only stores it
    # after verifying the token matches this exact row. Nullable because
    # linking is opt-in; a reviewer who never messages the bot simply never
    # gets Telegram notifications and keeps using the dashboard only, same
    # as before this feature existed. Unique so one Telegram account can't
    # accidentally end up linked to two reviewers at once.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    # Set once, out-of-band (an admin/seed script, not any web form this app
    # exposes), when a reviewer wants plain email notifications for approvals
    # they're entitled to decide (see app/notifications/email.py). Nullable
    # and unenforced-unique deliberately: unlike telegram_chat_id, there's no
    # verification step proving the reviewer controls this address, so it's
    # purely a "where to send a courtesy notification" field, never itself an
    # auth credential — deciding an approval always still requires the real
    # X-Reviewer-Token, exactly as before this field existed.
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiClientRole(str, enum.Enum):
    ADMIN = "admin"
    STANDARD = "standard"


class ApiClient(Base):
    """A caller of the HTTP API — replaces the single shared API_KEY string
    compare with a real per-caller identity (Reviewer's pattern, applied to
    the OUTER layer of auth). Added after a security review found that one
    shared key meant "may submit tickets" was indistinguishable from "may
    read every employee's audit trail/access history company-wide" —
    app/api/main.py's list_approvals/get_ticket_audit routes previously had
    no way to scope a read to "tickets THIS caller filed."

    `admin` clients see everything (today's behavior, for the ops/reviewer
    UI). `standard` clients may only read tickets/audit entries/approvals
    they themselves submitted, scoped by Ticket.submitted_by_client_id (NOT
    Ticket.requester, a free-text field the caller controls — see
    Ticket.submitted_by_client_id's docstring for why that distinction is
    load-bearing) — see app/api/main.py's scoping checks on
    GET /tickets/{id}, GET /tickets/{id}/audit, and GET /approvals.

    `daily_request_count`/`request_count_reset_at` back a simple per-client
    daily cap on POST /tickets (app/api/main.py) — a request-count budget,
    not true LLM token accounting: attributing actual token spend to a
    specific caller would require threading this identity through
    AgentState/the LangGraph checkpointer (resumed across HITL pauses,
    potentially hours later) and every LLM call site in app/agent/graph.py,
    a substantially larger change than the auth/scoping work here. A
    request-count cap bounds the same "one caller submits unbounded
    maximum-length tickets in a loop" cost-amplification pattern the
    security review flagged, without that plumbing.
    """

    __tablename__ = "api_clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    role: Mapped[ApiClientRole] = mapped_column(Enum(ApiClientRole), default=ApiClientRole.STANDARD)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=_default_api_client_key)
    daily_request_limit: Mapped[int] = mapped_column(default=100)
    daily_request_count: Mapped[int] = mapped_column(default=0)
    request_count_reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Same reset-on-read shape as daily_request_count/request_count_reset_at
    # above, denominated in LLM tokens instead of requests — the org-level
    # cost-governance half of Settings.max_tokens_per_ticket (that setting
    # bounds one ticket's spend; this bounds one client's/the org's DAILY
    # spend across every ticket). Updated from app/agent/token_budget.py's
    # record_client_spend_and_check_budget, called at the same plan/replan
    # checkpoints that already persist a ticket's running token total into
    # AgentState — NOT threaded through the checkpointer itself, since
    # Ticket.submitted_by_client_id (looked up fresh by ticket_id at that
    # point) already answers "which client owns this spend" without a
    # checkpoint schema change.
    #
    # Nullable, unlike daily_request_count/request_count_reset_at above,
    # for the same reason data_last_purged_at/owned_by_client_id elsewhere
    # in this model are nullable: those columns were present from this
    # table's CREATE TABLE (the initial migration), so a NOT NULL default
    # was safe there; these two are added via ALTER TABLE to a table that
    # may already hold rows, and Postgres rejects a NOT NULL ADD COLUMN
    # with no server-side default against a non-empty table. Application
    # code (token_budget.py) treats NULL identically to 0/"never reset."
    tokens_used_today: Mapped[int | None] = mapped_column(nullable=True)
    token_count_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A DIFFERENT concern from request_count_reset_at above, despite the
    # similar shape: that field tracks the daily REQUEST-COUNT budget
    # resetting; this one tracks when this client's own tickets/approvals/
    # audit entries were last hard-deleted (app/agent/demo_purge.py) — "the
    # public demo resets every day" so its data doesn't accumulate forever
    # alongside real tickets. Nullable because most ApiClients (every real
    # one) never have this purge run against them at all — it's currently
    # only ever invoked for the one DEMO_API_KEY-seeded client.
    data_last_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    # WHO actually authenticated the POST /tickets call that created this
    # row — nullable because API_KEY-unset (local demo mode) submissions
    # have no ApiClient at all, and because pre-existing rows from before
    # this column existed have no way to backfill it. Deliberately NOT the
    # same thing as `requester` above: requester is free-text the caller
    # puts in the request BODY (e.g. "hr@example.com" as who the ticket is
    # ostensibly filed on behalf of) and is under the caller's full control
    # — nothing ties it to which credential actually made the call. A
    # security-review follow-up found this the hard way on a live public
    # demo: app/api/main.py's ticket/audit/approval read-scoping compared
    # `Ticket.requester == client.name`, so a caller submitting with a
    # `requester` value that didn't happen to equal their own ApiClient's
    # `name` (the demo client is literally named "public-demo-guest", never
    # equal to any requester a visitor would type) couldn't see their OWN
    # just-submitted ticket, and conversely nothing stopped one caller from
    # seeing another's ticket by guessing/matching the right requester
    # string. This column is the real ownership link; requester stays
    # purely descriptive.
    submitted_by_client_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_clients.id"), nullable=True
    )
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
    # HOW the deciding reviewer authenticated ("token" = per-reviewer shared
    # secret, "oidc" = IdP-verified JWT) and, for OIDC, the IdP's immutable
    # `sub` identifier — provenance that turns "someone presented mchen's
    # token" and "the IdP vouched this was mchen" into distinguishable
    # audit facts. Nullable: rows decided before these columns existed, and
    # rows still PENDING, legitimately have neither.
    reviewer_auth_method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reviewer_oidc_subject: Mapped[str | None] = mapped_column(String(128), nullable=True)
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
    """Record of every tool invocation the MCP server executed — chained via
    prev_hash/entry_hash (see app/db/audit.py::append_audit_log, the only
    intended writer) into a tamper-EVIDENT log: editing or deleting a row
    after the fact breaks the chain in a way verify_audit_chain() can
    detect. Not tamper-PROOF — a DB-admin-level actor who rewrites the
    entire chain from some point forward, recomputing every later hash
    consistently, would go undetected by this alone. See app/db/audit.py's
    module docstring for the full caveat and the recommended defense in
    depth (streaming to an external SIEM).
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    actor: Mapped[str] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(64))
    tool_args: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # This row's position in the hash chain: prev_hash is the chain tip's
    # hash AT THE TIME this row was appended; entry_hash covers prev_hash
    # plus every other column above. Nullable because rows written before
    # this chaining existed have neither (same "historical NULL" shape as
    # Approval.reviewer_auth_method) — verify_audit_chain() treats a NULL
    # entry_hash as "predates chaining, not verifiable, not tampering."
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    ticket: Mapped["Ticket | None"] = relationship(back_populates="audit_entries")


class AuditChainHead(Base):
    """Singleton (id is always 1) pointer to the audit hash chain's current
    tip — app/db/audit.py's append_audit_log() locks this ROW (not the
    whole audit_log table) to serialize concurrent chain appends across
    replicas, and verify_audit_chain() compares it against the last row's
    recomputed hash to catch a deleted trailing row (a deletion leaves no
    mismatched row behind, but the head then points at a hash nothing
    remaining reproduces).
    """

    __tablename__ = "audit_chain_head"

    id: Mapped[int] = mapped_column(primary_key=True)
    latest_hash: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
