# Runbooks

Operational procedures, and one section per alert rule in
[observability/alert-rules.yml](../observability/alert-rules.yml). The
observability stack itself: `docker compose -f docker-compose.yml -f
docker-compose.observability.yml up -d` → Grafana :3000, Prometheus :9090,
Alertmanager :9093.

## Deploy / rollback

- **Continuous**: merge to main → CI gate → Trivy-scanned image pushed to
  GHCR `:latest` + `:sha` (deploy.yml). Render auto-deploys from main.
- **Versioned**: `git tag v1.2.3 && git push --tags` → release.yml builds,
  scans, pushes `:1.2.3`, opens a GitHub Release.
- **Rollback** = run the previous tag. Compose: pin `image:` to the prior
  version. Helm: `helm rollback <release>` or `--set image.tag=1.2.2`.
  Schema: migrations are additive so far; a rollback that must undo one
  runs `alembic downgrade <rev>` — check the migration's `downgrade()`
  before assuming it's lossless.
- **Schema changes**: edit models → `alembic revision --autogenerate -m
  "..."` → review the generated DDL → CI's drift gate
  (tests/test_migrations.py) proves models == migrations. K8s applies them
  via the pre-upgrade Job; compose/Render deployments run
  `alembic upgrade head` (or rely on init_db()'s self-healing for
  SQLite-scale changes).

## Before changing models/prompts/providers

Run the live eval against the real model: `python -m evals.run_live`
(EVAL_MIN_SCORE=0.8 default). CI replays recorded outputs and cannot see
model drift — this can. Budget ~2 minutes and a few thousand tokens.

Worked example (2026-07-13, the run that set the current model pin):
`llama-3.1-8b-instant` scored **3/6** — hallucinated a cross-domain tool
name (`access_get_user`), classified a status inquiry as ONBOARDING, padded
plans with unrequested grants — while every sensitive step still gated and
the injection case still refused (safety held; planning quality didn't).
`llama-3.3-70b-versatile` on the same key scored **5/6** (one cosmetic miss:
`"VPN"` capitalization as a resource name), so `.env`/`render.yaml` pin the
70b model. That's the shape of a useful eval read: separate the safety
signals (gating, forbidden tools) from the quality signals (category, plan
shape) before deciding whether a score change blocks a rollout.

## Backup / restore

- **Postgres**: `pg_dump -Fc it_automator > backup.dump`; restore with
  `pg_restore -d it_automator backup.dump`. The LangGraph checkpoints
  tables are in the same database — one dump covers app state AND paused
  HITL runs. Schedule via cron/managed-PG snapshots; test restores
  quarterly.
- **SQLite (dev)**: copy `data/*.db` while the app is stopped.

## Data retention

- Idempotency keys: purged automatically past TTL by the SLA sweep.
- Demo-client data: hard-deleted on the DEMO_DATA_RESET_HOURS cadence.
- Tickets/approvals/audit: kept indefinitely by design (audit trail is an
  asset). If a deployment needs retention limits, add a sweep step —
  deleting a ticket should also call `delete_ticket_thread()` so its
  checkpoint rows go with it (see demo_purge.py for the pattern).

---

## Alerts

### HighHttpErrorRate
> \>5% of requests 5xx for 5 minutes.

1. `docker compose logs app --since 15m | grep '"level": "ERROR"'` (JSON
   logs carry request ids — pull one and grep its full trail).
2. Common causes seen live: LLM provider down (see LLM errors in logs —
   check provider status page), DB unreachable (`/ready` says which check
   fails), a bad deploy (rollback per above).

### ApprovalsPendingPileup
> approvals_pending > 10 for 30m.

Reviewers aren't deciding. Check notification delivery (Telegram/SMTP
config), check reviewers exist for the affected targets
(`GET /approvals?status=pending` → who is entitled), worst case decide via
dashboard as an it_admin. If pending never drains despite decisions, the
sweep may be failing — check its log lines.

### ApprovalEscalations
> SLA breaches in the last hour.

Same investigation as pileup; escalation is the SLA sweep flagging
individual approvals. Escalated approvals still need a human decision —
they are NOT auto-rejected.

### CircuitBreakerOpen
> An MCP domain tripped for 5m+.

`curl :8000/health` shows per-domain breaker state. The domain's backend
(in this demo, the gateway process itself; in a real integration, the
Jira/IdP API behind it) is failing. Fix/restart the backend; the breaker
half-opens after its recovery timeout and closes on the next success.

### StuckTicketsDetected
> Tickets failed by the stuck-ticket sweep.

Means a process died mid-run (deploy during a run, crash). The tickets are
already FAILED with an explanatory summary — resubmit if still needed.
Investigate what killed the process: OOM (check container restarts),
deploy timing (long runs + short graceful-timeout), panics in logs.

### TokenBudgetAborts
> Runs aborted on MAX_TOKENS_PER_TICKET.

Look at the failed ticket's audit trail: repeated replans indicate the
planner is looping on a contradiction (that's the budget doing its job);
a single plan blowing the budget means the budget is undersized for
current prompts — raise it deliberately.

### ApiDown
> /metrics unscrapable 2m.

The app is down or wedged. `docker compose ps` / `kubectl get pods`;
container restarting → check its exit reason; running but unresponsive →
grab `/health` and thread dumps (py-spy) before restarting.

### LlmFallbackActive
> LLM calls served by a non-primary provider.

Tickets are still completing (that's the whole point of the fallback —
see app/agent/llm.py's ainvoke_with_fallback), but the configured
LLM_PROVIDER is degraded/down. Check the primary provider's own status
page (Groq/Anthropic/watsonx/OpenRouter) and its API key validity first —
a bad/expired key looks identical to an outage from this alert's
perspective. This is a warn, not a page: no ticket is failing right now,
but sustained fallback traffic means you're running on a different
model's quality/latency/cost profile than you intended, and every model
served by the fallback needs credentials configured (blank ones are
silently skipped, not attempted) — confirm the fallback provider you're
now depending on actually has a valid key set, not just the primary.
