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

Also trigger the adversarial red-team eval
(`.github/workflows/red-team.yml`, `workflow_dispatch`, or locally via
`python -m evals.run_adversarial`) — a model/prompt change is exactly the
kind of change that can silently weaken prompt-injection resistance
without touching anything the golden-ticket suite's single pinned
injection case would catch. Default `ADVERSARIAL_MIN_SCORE=1.0`: unlike
the golden suite's live-model tolerance for one quality miss, a
regression here means a guardrail stopped holding, not a flaky quality dip.

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

## After changing an MCP tool's name, description, or arguments

`tests/test_tool_baseline.py` fails CI if the live-discovered tool set
(`app/mcp_server/*_server.py`) disagrees with the committed
`app/mcp_server/tool_baseline.json` (the tool-poisoning defense —
app/agent/tool_integrity.py). **Do not** run
`python -m scripts.generate_tool_baseline` locally and commit its output —
that produced two real, confirmed false-positive CI failures (pydantic-core
schema-formatting differences between a dev machine and CI's locked
environment, nothing to do with an actual tool change). Instead, trigger
`.github/workflows/regenerate-tool-baseline.yml` from the Actions tab ("Run
workflow") — it regenerates the file inside CI's own environment and
commits the result, which is the only way to guarantee the committed file
matches what the drift gate itself will compute. Review the diff it posts
to the run's step summary before/after — a mismatch limited to the tool(s)
you actually meant to change is expected; anything else is worth
investigating before merging.

## Backup / restore

- **Postgres, manual**: `pg_dump -Fc it_automator > backup.dump`; restore
  with `pg_restore -d it_automator backup.dump`. The LangGraph checkpoints
  tables are in the same database — one dump covers app state AND paused
  HITL runs.
- **Postgres, automated**: `scripts/backup_db.sh`, or the Helm chart's
  `backup-cronjob.yaml` (`Values.backup.enabled`, disabled by default).
  Both read a **separate** `BACKUP_DATABASE_URL` — never `DATABASE_URL`,
  the app's own read/write credential. This is a hard requirement, not a
  convenience: an agent run with valid, approved credentials can delete
  production data, and a backup plane that trusts the same credential as
  the app it's protecting is not a real recovery plane. Provision the
  backup role with **`SELECT` only** — no `INSERT`/`UPDATE`/`DELETE`/`DROP`
  on the app's tables:
  ```sql
  CREATE ROLE backup_ro LOGIN PASSWORD '...';
  GRANT CONNECT ON DATABASE it_automator TO backup_ro;
  GRANT USAGE ON SCHEMA public TO backup_ro;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_ro;
  ```
  For Kubernetes, the CronJob's `BACKUP_DATABASE_URL` lives in a
  **different** `Secret` (`Values.backup.existingSecret`) than the app's
  own `existingSecret`, provisioned/rotated independently — see
  `values.yaml`'s `backup` block.
- **SQLite (dev)**: copy `data/*.db` while the app is stopped.
- **Restore drills**: `scripts/validate_backup_restore.sh` proves a
  produced dump is genuinely restorable — creates a scratch read-only
  role, runs `backup_db.sh` as that role (confirming it truly can't
  write), restores into a fresh database, and checks the Alembic revision
  and a canary row match the source. Run it quarterly, and any time the
  schema or the backup role's grants change. "A file exists in the backup
  bucket" is not the same claim as "we can recover from it" — this script
  is what actually proves the second one.

## Audit log integrity

`AuditLog` rows are hash-chained (`app/db/audit.py`) — every row's
`entry_hash` covers the previous row's hash plus its own fields, and a
singleton `audit_chain_head` row tracks the chain's current tip. This is
**tamper-evident, not tamper-proof**: it detects a row edited or deleted
after the fact by anyone who doesn't also recompute every later hash to
match, but a DB-admin-level actor rewriting the whole chain forward from a
tampering point *and* recomputing consistent hashes would not be caught by
this alone. Treat it as one layer, not the whole story — the recommended
defense in depth (not yet built) is streaming `AuditLog` rows to an
external SIEM/append-only log as they're written, so a compromised DB
credential can't also erase the evidence of its own use.

- **Check integrity on demand**: `GET /audit/verify` (ADMIN API client
  only) walks the full chain and returns `{"ok": bool, "detail": str}`. A
  `false` result is itself logged as an `audit_chain_verification_failed`
  security event.
- **If it reports tampering**: treat it as a real incident, not a bug
  report — identify which row/range failed (the `detail` message names the
  first mismatched row, or reports the chain head disagreeing with the
  last row, which means trailing rows were deleted), pull `GET
  /audit/export` for the surrounding time range from a point BEFORE the
  reported row, and cross-reference against `GET /audit/export`'s own
  security events (`audit_log_exported`) and any external SIEM feed if one
  is configured, to establish who had DB access in that window.
- **Rows from before this existed** have `entry_hash IS NULL` and are
  skipped by verification (not flagged as tampered) — this is expected for
  any database that existed before the `audit hash chain` migration
  landed, not a sign of a gap.
- Recommended cadence: run `GET /audit/verify` as part of the same
  quarterly restore-drill routine below, not just reactively.

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

### OrgTokenBudgetNearCap
> Org-wide daily LLM spend past 80% of MAX_ORG_TOKENS_PER_DAY.

Check `org_tokens_used_today` on the Grafana dashboard against
`org_token_budget_limit`. Not itself an incident — a heads-up before
`ClientTokenBudgetExceeded` starts firing and legitimate tickets get
rejected. Either raise `MAX_ORG_TOKENS_PER_DAY` if the spend is expected
(traffic growth), or check for one client running away with disproportionate
usage (`GET /audit/export` filtered to the suspect client's tickets).

### ClientTokenBudgetExceeded
> A ticket was rejected/aborted on a per-client or org-wide daily token cap.

Expected behavior under sustained load near the configured cap, not
necessarily a bug. If it's firing for a single legitimate client
repeatedly, either raise `MAX_TOKENS_PER_CLIENT_PER_DAY` for that
deployment or investigate why that client's tickets are consuming more
tokens than expected (a replanning loop shows up here too, same as
`TokenBudgetAborts` above, just attributed at the client level instead of
per-ticket).

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
