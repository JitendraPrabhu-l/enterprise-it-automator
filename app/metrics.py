"""Prometheus metrics for the API and agent — the metrics half of Stage 3.4's
observability story (app/observability.py is the tracing half; the two are
deliberately separate signals: traces answer "what happened inside THIS
run," metrics answer "what is the rate/latency/error budget over ALL runs,"
which is what dashboards and alert rules consume).

Metric objects live at module level in prometheus_client's default REGISTRY
and are incremented from the same call sites the tracing helpers already
instrument (app/observability.py's record_llm_call/record_tool_call), plus
the HTTP middleware and the domain-event sites in app/api/main.py and
app/agent/sla_sweep.py. Everything degrades to plain in-process counters
with no collector configured — like tracing, always safe to leave in place.

Multiprocess note: the Dockerfile serves with 2 gunicorn workers, each its
own process with its own registry — a bare /metrics would show one worker's
counters at random per scrape. prometheus_client's standard fix is mmap
files shared across workers, enabled ONLY when PROMETHEUS_MULTIPROC_DIR is
set in the environment (render_metrics() then aggregates across processes).
Local `uvicorn` dev and pytest run single-process with the env var unset and
use the default registry directly. Gauges carry multiprocess_mode so they
stay meaningful when aggregated ("max" — for a pending-approvals level and
a breaker-open flag, the worst worker's view is the operationally honest
one; summing across workers would double-count a DB-level fact).
"""

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests served, by method, route template, and status code.",
    ["method", "path", "status"],
)

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency by method and route template.",
    ["method", "path"],
    # /tickets synchronously runs an LLM-driven agent to completion or first
    # HITL interrupt — tens of seconds is a NORMAL request here, so the
    # default 10s-max buckets would lump every agent run into +Inf.
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 30.0, 60.0, 120.0),
)

TICKETS_SUBMITTED = Counter(
    "tickets_submitted_total",
    "Tickets accepted by POST /tickets (before the agent run starts).",
)

TICKETS_FINALIZED = Counter(
    "tickets_finalized_total",
    "Tickets that reached a terminal status in finalize, by status.",
    ["status"],
)

APPROVALS_DECIDED = Counter(
    "approvals_decided_total",
    "Human approval decisions recorded, by outcome (approved/rejected).",
    ["decision"],
)

APPROVALS_ESCALATED = Counter(
    "approvals_escalated_total",
    "Approvals escalated by the SLA sweep after sitting PENDING past deadline.",
)

APPROVALS_PENDING = Gauge(
    "approvals_pending",
    "Approvals currently awaiting a human decision (refreshed each SLA sweep pass).",
    multiprocess_mode="max",
)

STUCK_TICKETS_FAILED = Counter(
    "stuck_tickets_failed_total",
    "Tickets the SLA sweep marked FAILED after being stuck in planning/executing.",
)

LLM_CALLS = Counter(
    "llm_calls_total",
    "LLM invocations, by model.",
    ["model"],
)

LLM_TOKENS = Counter(
    "llm_tokens_total",
    "LLM tokens consumed, by model and direction (input/output).",
    ["model", "direction"],
)

TICKET_TOKEN_BUDGET_EXCEEDED = Counter(
    "ticket_token_budget_exceeded_total",
    "Ticket runs aborted because they exceeded MAX_TOKENS_PER_TICKET.",
)

CLIENT_TOKEN_BUDGET_EXCEEDED = Counter(
    "client_token_budget_exceeded_total",
    "Ticket runs aborted (at submission or mid-run) because a per-client "
    "or org-wide daily token budget (MAX_TOKENS_PER_CLIENT_PER_DAY / "
    "MAX_ORG_TOKENS_PER_DAY) was already met or exceeded.",
)

ORG_TOKENS_TODAY = Gauge(
    "org_tokens_used_today",
    "Sum of every ApiClient's tokens_used_today — the org-wide daily LLM "
    "token spend MAX_ORG_TOKENS_PER_DAY is checked against. Refreshed "
    "whenever the client/org budget check runs (submission-time or the "
    "plan/replan runtime gate), not on a separate timer.",
    multiprocess_mode="max",
)

ORG_TOKEN_BUDGET_LIMIT = Gauge(
    "org_token_budget_limit",
    "The currently-configured MAX_ORG_TOKENS_PER_DAY value, exported as its "
    "own gauge so the OrgTokenBudgetNearCap alert rule can express a RATIO "
    "(org_tokens_used_today / org_token_budget_limit) in PromQL, which has "
    "no way to read application config directly. 0 when unconfigured.",
    multiprocess_mode="max",
)

MCP_TOOL_CALLS = Counter(
    "mcp_tool_calls_total",
    "MCP tool calls, by tool name and outcome (success/failure).",
    ["tool", "outcome"],
)

CIRCUIT_BREAKER_OPEN = Gauge(
    "mcp_circuit_breaker_open",
    "1 when the named MCP domain's circuit breaker is open or half-open, else 0.",
    ["domain"],
    multiprocess_mode="max",
)

MCP_TOOL_BASELINE_MISMATCH = Counter(
    "mcp_tool_baseline_mismatch_total",
    "Live-discovered MCP tool definitions that drifted from the committed "
    "tool_baseline.json (app/agent/tool_integrity.py) — a nonzero rate "
    "means either an unreviewed server-side tool change or a real "
    "tool-poisoning attempt; see docs/RUNBOOKS.md.",
)

LLM_FALLBACK_TOTAL = Counter(
    "llm_fallback_total",
    "LLM calls served by a NON-primary provider after the configured "
    "primary failed — see app/agent/llm.py's ainvoke_with_fallback. Any "
    "sustained nonzero rate here means the configured LLM_PROVIDER is "
    "degraded/down and traffic is running on a fallback.",
    ["primary", "served_by"],
)


def render_metrics() -> tuple[bytes, str]:
    """Returns (payload, content_type) for GET /metrics. In multiprocess
    mode (PROMETHEUS_MULTIPROC_DIR set — see module docstring) aggregates
    across every worker's mmap files via a throwaway registry, exactly as
    prometheus_client's own multiprocess docs prescribe; otherwise serves
    the default in-process registry.
    """
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
