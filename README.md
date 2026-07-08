# MCP-Enabled Enterprise IT Automator

[![CI](https://github.com/JitendraPrabhu-l/enterprise-it-automator/actions/workflows/ci.yml/badge.svg)](https://github.com/JitendraPrabhu-l/enterprise-it-automator/actions/workflows/ci.yml)
[![Build and Push Image](https://github.com/JitendraPrabhu-l/enterprise-it-automator/actions/workflows/deploy.yml/badge.svg)](https://github.com/JitendraPrabhu-l/enterprise-it-automator/actions/workflows/deploy.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Live](https://img.shields.io/website?url=https%3A%2F%2Fenterprise-it-automator.onrender.com%2Fhealth&up_message=online&down_message=asleep&label=live)](https://enterprise-it-automator.onrender.com/)

A multi-agent-style IT automation system that processes employee onboarding/offboarding
tickets by reasoning over a custom **Model Context Protocol (MCP)** server, with
**human-in-the-loop (HITL)** approval enforced server-side for sensitive actions.

**Live:** [enterprise-it-automator.onrender.com](https://enterprise-it-automator.onrender.com/)
— the dashboard auto-fills a public, low-privilege demo key so you can submit a ticket
without asking for a credential (may take ~30–60s to wake up on the free tier's first
request after idling — the badge above will read "asleep" until then).

![Dashboard screenshot](docs/dashboard-screenshot.png)

## Quick start

```bash
git clone https://github.com/JitendraPrabhu-l/enterprise-it-automator.git
cd enterprise-it-automator
python -m venv .venv && source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r requirements.txt

cp .env.example .env
# edit .env: set GROQ_API_KEY (free: https://console.groq.com/keys) and API_KEY (any secret string)

python -m app.db.seed                 # seeds mock employees + reviewer tokens (printed once)
python -m uvicorn app.api.main:app --reload
```

Open `http://127.0.0.1:8000/` for the dashboard, or `http://127.0.0.1:8000/docs` for the
interactive API reference. See **[Setup](#setup)** below for the full walkthrough, or
**[Deployment](DEPLOYMENT.md)** to run this against a real Postgres database on Render.

Prefer containers? `docker compose up --build` — see **[Running with Docker](#running-with-docker)**.

## Contents

- [Architecture](#architecture)
- [Setup](#setup)
- [MCP transport: local vs. remote](#mcp-transport-local-vs-remote)
- [Example flow](#example-flow)
- [Live streaming via AG-UI](#live-streaming-via-ag-ui)
- [Running with Docker](#running-with-docker)
- [Observability](#observability)
- [Secrets management](#secrets-management)
- [Identity &amp; approval authorization](#identity--approval-authorization-scoped-down-stage-4)
- [MCP discovery, PII masking &amp; prompt-injection guardrails](#mcp-discovery-pii-masking--prompt-injection-guardrails)
- [Tests](#tests)
- [Notes on model choice](#notes-on-model-choice)
- [Contributing](CONTRIBUTING.md)
- [Deployment guide](DEPLOYMENT.md)

## Architecture

```
                 ┌─────────────┐        JSON-RPC over stdio        ┌────────────────────┐
  HTTP ticket ─▶ │  FastAPI    │ ─▶ LangGraph agent ─▶ MCP client ─▶│  Custom MCP server  │
                 │  (api/main) │        (agent/graph)                │  (mcp_server/server)│
                 └──────┬──────┘                                     └──────────┬──────────┘
                        │                                                       │
                        ▼                                                       ▼
                 Approvals / Tickets                                   Mock enterprise identity
                 (Postgres/SQLite)                                     store + audit log (same DB)
```

- **`app/mcp_server/`** — a real MCP server (built on the official `mcp` Python SDK)
  composing three domain servers (identity, access, ticketing) behind one
  gateway, each tool exposed under a domain-prefixed name
  (`identity_get_user`, `identity_create_user`, `identity_disable_user`,
  `access_grant_access`, `access_revoke_access`, `ticketing_add_ticket_comment`,
  `ticketing_get_ticket_status`) over JSON-RPC, on either of two transports
  (see **MCP transport: local vs. remote** below). `identity_create_user` auto-grants
  a default access bundle based on the employee's `department`
  (`DEPARTMENT_ACCESS_DEFAULTS` in `tools.py` — e.g. Engineering gets
  `vpn`, `github:engineering`, `jira:core-platform`; IT additionally gets
  `admin-panel`; unmapped departments get `vpn` only), so onboarding tickets
  don't need to spell out every resource. Sensitive tools
  (`identity_disable_user`, `access_revoke_access`) require a server-verified `approval_id` —
  the server itself refuses the call if no human has approved that *exact*
  tool + arguments combination (`approval_gate.py`). This is a real security
  boundary, not a prompt-level suggestion the LLM could talk its way past, and
  it is enforced identically regardless of which transport the client used to
  reach the server.
- **`app/agent/`** — a [LangGraph](https://langchain-ai.github.io/langgraph/) DAG:
  `plan → route → execute_step → route → ... → finalize`, with a dedicated
  `await_approval` node that uses LangGraph's `interrupt()` to pause the whole
  graph (checkpointed) whenever the next planned step is sensitive and
  unapproved. A separate HTTP request resumes it later — this models a
  realistic multi-hour approval turnaround, not just a synchronous callback.

  - `llm.py` is a pluggable adapter —
    `LLM_PROVIDER=groq|anthropic|watsonx|openrouter` swaps the backend with
    no code changes, so the same graph can run on a free Groq key today and
    point at IBM Granite via watsonx (or a credential-free OpenRouter free
    model, if watsonx provisioning is blocked) later.
  - The planner enforces a **structural JSON guardrail**: `_extract_json_array`
    strips markdown fences/prose and raises a clear error (routed to a
    `FAILED` ticket state, not a crash) if the model doesn't return valid JSON,
    plus a hard cap on plan size (`MAX_PLAN_LENGTH`) and a format check on
    every step's `username` arg (`_USERNAME_PATTERN`) — see **MCP discovery,
    PII masking & prompt-injection guardrails** below.
  - **Real MCP discovery, not a hardcoded tool list**: `discover_tool_reference()`
    calls the live `tools/list` endpoint on every `plan`/`replan` and formats
    whatever the server currently exposes into the prompt — the actual point
    of MCP's discovery phase (the client asks the server what's available,
    instead of a hand-maintained string that can silently drift from what's
    really being served).
- **`app/api/`** — FastAPI endpoints to submit tickets, list/inspect them,
  list/decide pending approvals, pull the per-ticket audit trail, and list
  current/past employees (`GET /employees`, `?status=active|disabled`). All
  of these (except `/health` and the static UI page) require an `X-API-Key`
  header matching `API_KEY` in `.env` — see **Auth** below.
- **`app/db/`** — SQLAlchemy models: `EmployeeUser` (mock IBM ID Management
  record), `Ticket`, `Approval`, `AuditLog`. SQLite by default (zero setup) —
  both the app DB and the LangGraph checkpoint DB live under `data/` (created
  automatically on first run if missing); swap `DATABASE_URL` for Postgres in
  production.
- **MCP server architecture**: a real server built from scratch, registered
  and driven by an external client over either of two transports — the same
  local (stdio) or remote (streamable-HTTP) integration paths an orchestrator
  like watsonx Orchestrate supports when registering an MCP tool server.
- **Security or enterprise auth flow**: sensitive tool calls require a
  pre-issued, single-use approval token verified server-side against tool
  name *and* arguments — prevents replay/reuse across different actions.
- **ReAct-style reasoning + guardrails**: the planner reasons step-by-step
  over the ticket, and malformed LLM output degrades to a clean `FAILED`
  ticket state instead of an unhandled exception.
- **State management in long-running workflows**: the LangGraph checkpointer
  persists agent state across the interrupt/resume boundary, so approval can
  happen minutes or hours after the ticket was submitted, from a completely
  separate HTTP request — and across an application restart, since the
  checkpointer is backed by a SQLite file (`CHECKPOINT_DB_PATH`), not kept
  only in memory.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt

cp .env.example .env
# then set GROQ_API_KEY (free key: https://console.groq.com/keys)
# and set API_KEY to any secret string (required outside of local demo use)
```

### Auth

Every endpoint except `/health` and the static UI page (`GET /`) requires an
`X-API-Key` header matching `API_KEY` in `.env`:

```bash
curl -X POST http://127.0.0.1:8000/tickets -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{...}'
```

The built-in dashboard prompts for the key once (on first load) and stores it
in `localStorage`. If `API_KEY` is left blank, auth is disabled and the server
logs a warning at startup — only do this for a fully local, single-user demo.

For a public deployment you want strangers to be able to try without asking
you for a credential, set `DEMO_API_KEY` to a separate value — the dashboard
auto-fills it for any visitor with no key of their own. It's seeded as a
low-privilege client (can submit tickets, sees only tickets it filed itself,
capped at 10 requests/day), never your real key. Its own tickets/approvals
are hard-deleted once a day and hidden from your admin key's default view in
the meantime, so demo traffic never mixes with real data. See `.env.example`
and `DEPLOYMENT.md`.

Seed a couple of mock employees and run the API:

```bash
python -m app.db.seed
python -m uvicorn app.api.main:app --reload
```

Open `http://127.0.0.1:8000/` for the minimal web UI (submit tickets, approve/reject
pending sensitive actions, browse current/past employees, inspect the audit log), or
`http://127.0.0.1:8000/docs` for the interactive Swagger UI. Both talk to the same
JSON endpoints.

The UI is a single static page (`app/static/index.html`, vanilla HTML/CSS/JS,
no build step) served directly by the FastAPI app — polls `/tickets`,
`/approvals?status=pending`, and `/employees` every 8s and on every action.

The "Clear" button on the Tickets panel hides tickets from that browser's
view only — it does not delete anything server-side. Hidden ticket IDs are
tracked in `localStorage`; "Show cleared (N)" toggles them back into view,
and each cleared ticket has a "Restore" action while shown.

### MCP transport: local vs. remote

The MCP server (`app/mcp_server/server.py`) supports two transports, controlled
by `MCP_TRANSPORT` in `.env` (or `--transport` on the CLI) — same tools, same
`approval_gate` enforcement, either way:

- **`stdio` (default, local)** — the agent process spawns the MCP server as a
  subprocess and talks to it over a stdio pipe. Zero config, nothing to run
  separately; this is what `app/agent/mcp_client.py` does out of the box.
- **`http` (remote)** — the MCP server runs standalone as its own long-lived
  process, listening on `MCP_SERVER_HOST:MCP_SERVER_PORT` and speaking MCP
  over streamable-HTTP. This is what a real deployment or an orchestrator
  such as watsonx Orchestrate means by registering a "remote MCP server":
  the server is a separate service reachable by URL, not something the
  caller launches itself.

Run the server standalone for inspection/debugging (stdio, the default):

```bash
python -m app.mcp_server.server
```

Speaks MCP JSON-RPC over stdio — connect with any MCP-compatible client or
the [MCP Inspector](https://github.com/modelcontextprotocol/inspector).

Run it as a standalone remote server over HTTP instead:

```bash
python -m app.mcp_server.server --transport http
# or: set MCP_TRANSPORT=http in .env and run with no flag
```

To point the agent at that remote server instead of spawning its own local
subprocess, set in `.env`:

```
MCP_TRANSPORT=http
MCP_SERVER_URL=http://127.0.0.1:8765/mcp
```

`app/agent/mcp_client.py` reads the same `MCP_TRANSPORT` setting and connects
over streamable-HTTP to `MCP_SERVER_URL` instead of spawning a subprocess —
the agent and the MCP server can then run as fully separate processes,
potentially on separate hosts.

#### Securing the HTTP transport for real deployment

The `stdio` transport needs none of this (the spawned subprocess inherits
trust from its parent process, per MCP spec 2025-11-25). The `http`
transport is reachable by any network client that can route to it, so
`app/mcp_server/server.py` layers on three independent protections —
**all three are already implemented; this is deployment guidance, not a
TODO list**:

1. **Bearer-token auth** (`MCP_SERVER_TOKEN`) — required or the transport
   refuses to start. FastMCP applies no authentication of its own.
2. **DNS-rebinding protection** (`MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS`)
   — the mcp SDK's own `TransportSecuritySettings`, validating the `Host`
   and `Origin` headers on every request. Defaults to loopback-only; set
   both explicitly if you front the gateway with a real hostname (see
   `docker-compose.yml`'s `mcp-server` service for a worked example using
   Docker's internal DNS name).
3. **Per-tool rate limiting** (`app/mcp_server/rate_limit.py`) — a token
   bucket per tool name, independent of the FastAPI layer's `slowapi`
   limits (which only cover `POST /tickets` and the approval-decision
   endpoint, not direct MCP tool calls).

**What none of the above covers: transport encryption.** Every example in
this README and in `docker-compose.yml` uses `http://`, which is fine on
`127.0.0.1` or over a trusted private Docker network, but means the bearer
token and all tool call arguments/results travel in **cleartext** the
moment `MCP_SERVER_URL` points anywhere else. If you expose the gateway
beyond localhost/a private network, terminate TLS in front of it — e.g. a
reverse proxy (nginx, Caddy, a cloud load balancer) handling HTTPS and
forwarding plaintext HTTP only over the loopback/private link to the
gateway — and change `MCP_SERVER_URL`/`MCP_TRANSPORT`-related config on
the agent side to point at the `https://` front door. This project doesn't
ship that proxy config (no code change required in `app/` either way —
it's purely how the process is fronted), the same "runtime concern, not
an app concern" pattern as **Secrets management** above.

## Example flow

**1. Onboarding (no sensitive actions — completes immediately):**

```bash
curl -X POST http://127.0.0.1:8000/tickets -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{
  "requester": "hr@example.com",
  "subject": "Onboard new hire",
  "body": "Onboard Kevin Lee (username klee, email klee@example.com, dept Engineering) and grant VPN access."
}'
```

**2. Offboarding (hits the HITL gate):**

```bash
curl -X POST http://127.0.0.1:8000/tickets -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{
  "requester": "hr@example.com",
  "subject": "Offboard employee",
  "body": "Offboard jsmith - disable her account, she left the company."
}'
# -> response has "interrupted": true and a "pending_approval" with an approval_id
```

**3. A human reviews and decides:**

```bash
curl -X POST http://127.0.0.1:8000/approvals/1/decide \
  -H "X-API-Key: $API_KEY" -H "X-Reviewer-Token: $REVIEWER_TOKEN" \
  -H "Content-Type: application/json" -d '{"approve": true}'
# -> graph resumes exactly where it paused; if the plan has more sensitive
#    steps it pauses again at the next one, otherwise the ticket completes.
# $REVIEWER_TOKEN authenticates you AS a specific reviewer (see "Identity &
# approval authorization" below) — python -m app.db.seed prints real tokens
# for the seeded reviewers (mchen, admin) the first time it creates them.
# An invalid/missing token is a 401; a valid token for a reviewer not
# entitled to decide this specific approval is a 403.
```

**4. Inspect what actually happened:**

```bash
curl http://127.0.0.1:8000/tickets/1/audit -H "X-API-Key: $API_KEY"
```

## Live streaming via AG-UI

`POST /tickets` and `POST /approvals/{id}/decide` block until the graph hits
its next interrupt or finishes, then return one JSON blob — fine for
scripts/curl, but a UI watching a ticket in progress has to poll (the
`app/static/index.html` dashboard polls `GET /tickets` every 8s). For a
live view, two additional endpoints stream the SAME graph run as
[AG-UI protocol](https://docs.ag-ui.com) events over Server-Sent Events,
using the official `ag-ui-protocol` PyPI package:

- `POST /tickets/stream` — streaming counterpart to `POST /tickets`
- `POST /approvals/{id}/decide/stream` — streaming counterpart to
  `POST /approvals/{id}/decide`

Both take the same request body/auth as their non-streaming counterparts
and emit `text/event-stream` frames (`app/agent/ag_ui_bridge.py`):

```
RUN_STARTED                        run begins (thread_id matches the
                                    LangGraph checkpoint thread, so both
                                    transports address the same run)
STEP_STARTED / STEP_FINISHED       one pair per graph node (classify, plan,
                                    execute_step, execute_batch_step, ...)
TOOL_CALL_START / TOOL_CALL_RESULT one pair per MCP tool call a node executes
STATE_DELTA                        JSON Patch (RFC 6902) fragments for
                                    plan_index/done/category, for "step N of
                                    M" progress without re-deriving it
RUN_FINISHED (outcome: success)     normal completion, carrying the same
                                    plan/results shape as RunResult
RUN_FINISHED (outcome: interrupt)   the graph paused for approval — carries
                                    an ag_ui.core.Interrupt (id = approval_id,
                                    tool_call_id = tool name, metadata =
                                    ticket_id/approval_id/tool/args) built
                                    directly from the same payload
                                    await_approval_node already passes to
                                    LangGraph's interrupt() — no parallel
                                    schema to keep in sync
RUN_ERROR                          an exception escaped the graph run
                                    entirely (a tool call's own failure is
                                    reported via TOOL_CALL_RESULT instead,
                                    same ok:false distinction RunResult uses)
```

Try it live:

```bash
curl -N -X POST http://127.0.0.1:8000/tickets/stream -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" -d '{
    "requester": "hr@example.com", "subject": "Grant access",
    "body": "Grant jsmith access to admin-panel"
  }'
# -> streams RUN_STARTED, STEP_*, STATE_DELTA, ... ending in RUN_FINISHED.
#    If a sensitive action needs approval, ends in RUN_FINISHED with
#    outcome.type "interrupt" instead — decide it the same way as the
#    non-streaming flow, then resume via the streaming endpoint:

curl -N -X POST http://127.0.0.1:8000/approvals/1/decide/stream \
  -H "X-API-Key: $API_KEY" -H "X-Reviewer-Token: $REVIEWER_TOKEN" \
  -H "Content-Type: application/json" -d '{"approve": true}'
```

`app/static/index.html`'s "Submit & Watch Live (AG-UI)" button and each
pending approval's "Approve & Watch" button drive these endpoints from the
browser — native `EventSource` can't attach the `X-API-Key`/
`X-Reviewer-Token` headers this API requires, so the frontend reads the SSE
body directly off a `fetch()` stream instead (see `readSseEvents` in
`app/static/index.html`), the same "data: `<json>`\n\n" framing
`EventSource` parses internally.

Not implemented: AG-UI's `TEXT_MESSAGE_*` / `REASONING_*` events (this
agent's LLM calls aren't token-streamed — each `ainvoke()` returns whole)
and `STATE_SNAPSHOT` (only incremental `STATE_DELTA` is emitted, since every
run starts from a fresh empty client-side state document rather than
resuming a previously-synced one across page reloads).

## Running with Docker

```bash
cp .env.example .env
# set at least GROQ_API_KEY, API_KEY, and MCP_SERVER_TOKEN in .env, then:
docker compose up --build
```

`MCP_SERVER_TOKEN` is required in this deployment shape specifically:
`docker-compose.yml`'s `mcp-server` service runs the gateway over
streamable-HTTP (`--transport http`), which refuses to start without a
bearer token configured — `docker compose up` fails fast with a clear
error if it's unset, rather than silently starting an unauthenticated MCP
server reachable from the Docker network.

Brings up three services: `postgres` (app DB), `mcp-server` (the MCP gateway
running standalone over streamable-HTTP — the "remote MCP server" shape),
and `app` (the FastAPI service, connecting to `mcp-server` over the network
instead of spawning it as a stdio subprocess). See `docker-compose.yml` for
the full environment wiring and a note on what's validated vs. not yet
(the app-DB-on-Postgres path is expected to work — `app/db/session.py` is
already database-agnostic — but hasn't been run against a live container in
this environment; the LangGraph checkpoint store deliberately stays on
SQLite for now, since `AsyncPostgresSaver` hasn't been wired into
`app/agent/runner.py` yet).

## Observability

Agent execution is instrumented with [OpenTelemetry](https://opentelemetry.io/)
(`app/observability.py`): every graph node (`classify`, `plan`,
`await_approval`, `execute_step`, `execute_batch_step`, `join_batch`,
`replan`, `finalize`) runs inside a `agent.node.<name>` span recording
wall-clock duration and success/error status, every LLM call records
token usage under the GenAI semantic convention attributes
(`gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`), and every MCP tool call records
`mcp.tool.name` / `mcp.tool.success` / `mcp.tool.domain` — the latter
right alongside the existing per-domain circuit breaker bookkeeping in
`app/agent/mcp_client.py`.

By default `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, so `configure_observability()`
leaves the global no-op tracer provider in place — every `start_as_current_span`
call throughout the app is then a cheap no-op, safe to leave instrumented in
local dev with nothing configured to receive spans. Point it at any OTLP-HTTP
collector to start exporting — e.g. a local
[Jaeger](https://www.jaegertracing.io/) instance, an OTel Collector forwarding
to Langfuse/LangSmith, or a hosted OTLP endpoint:

```
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces
```

No vendor SDK is imported directly — instrumentation always goes through
OTel's generic API, so swapping the exporter target is a config change, not
a call-site rewrite.

## Secrets management

`app/config.py`'s `Settings` (pydantic-settings) reads every credential from
environment variables (falling back to `.env` locally) regardless of *how*
those variables get set — so pointing the same app at a real secrets manager
in production requires no code change, only how the process is launched:

- **[Doppler](https://www.doppler.com/)** — `doppler run -- python -m uvicorn app.api.main:app`
  injects secrets as env vars at process start; nothing in `app/` needs to
  know Doppler exists.
- **[Infisical](https://infisical.com/)** — same pattern via `infisical run --`,
  or its Kubernetes operator injecting env vars/mounted files into the
  container directly.
- **Cloud-native equivalents** (AWS Secrets Manager + `secrets-manager-to-env`
  style init containers, GCP Secret Manager, Azure Key Vault) all reduce to
  the same shape: populate the environment before `app.config.get_settings()`
  is first called, then nothing downstream changes.

The one thing to avoid regardless of provider: never bake real secrets into
the Docker image (`.dockerignore` already excludes `.env`) or commit them to
`docker-compose.yml` — pass them at `docker run`/`docker compose up` time via
`--env-file` or the orchestrator's native secrets injection instead.

## Identity & approval authorization (scoped-down Stage 4)

Two pieces of "real identity & enterprise integration" are implemented in a
right-sized form rather than the full versions described in `ROADMAP.md`'s
Stage 4 — a real Keycloak/OIDC deployment, MCP OAuth 2.1, and a
SCIM/OpenLDAP-backed identity sync were all judged out of scope for a solo
project per the roadmap's own trap notes, and are documented as
deliberately skipped rather than silently missing:

- **Reviewer authentication** (`app/api/auth.py`'s `require_reviewer_token`)
  — each `Reviewer` row has its own per-reviewer secret `token`, generated
  by `python -m app.db.seed` and required via the `X-Reviewer-Token` header
  on `POST /approvals/{id}/decide`. This is what actually binds a decision
  to a specific person: an earlier version of this trusted a `reviewer`
  field in the request body, which was a self-asserted claim anyone
  holding the one shared `API_KEY` could set to any registered reviewer's
  name (including `admin`) — a real impersonation gap, since fixed. The
  request body no longer has a `reviewer` field at all.
- **Lightweight RBAC** (`app/api/rbac.py`) — layers an authorization check
  on top of the now-authenticated reviewer identity. A `reviewers` table
  assigns each reviewer a role (`it_admin` or `manager`);
  `POST /approvals/{id}/decide` restricts a `manager` reviewer to approvals
  targeting their own direct reports (`EmployeeUser.manager_username`) — an
  `it_admin` may decide any approval. This directly closes the "no RBAC
  concept anywhere in the schema" audit finding without standing up a real
  identity provider. Read-only endpoints (`GET /approvals`,
  `GET /tickets/{id}/audit`) are intentionally NOT scoped by this
  relationship — this is a small-team ops dashboard, not a multi-tenant
  system, and the real authorization boundary is who may *decide* an
  approval, not who may *view* one.
- **Approval replay prevention** (`app/mcp_server/approval_gate.py`) — an
  `Approval`'s `executed_at` is set the first time it authorizes a
  sensitive tool call; a second attempt to use the same `approval_id` is
  refused. Previously one human sign-off could authorize the underlying
  action an unlimited number of times (e.g. via direct MCP calls over the
  streamable-HTTP transport, bypassing the FastAPI layer's auth entirely).
- **MCP gateway bearer-token auth** (`app/mcp_server/server.py`) — when run
  with `--transport http`, the gateway now requires an `Authorization: Bearer <MCP_SERVER_TOKEN>` header on every request (a Starlette
  middleware wrapping `streamable_http_app()`). FastMCP applies zero
  authentication of its own to this transport by default, so without this
  any network client that could reach `MCP_SERVER_HOST:MCP_SERVER_PORT`
  could call sensitive tools directly. Not full OAuth 2.1 (ROADMAP.md Stage
  4.3, explicitly descoped) — a static shared token is the right-sized fix
  for "zero auth at all." The stdio transport doesn't need this (the
  spawned subprocess inherits trust from its parent process). See
  **Securing the HTTP transport for real deployment** above for the other
  two protections layered alongside this (DNS-rebinding validation,
  per-tool rate limiting) and TLS guidance.
- **Approval SLA timeout + stuck-ticket detection** (`app/agent/sla_sweep.py`)
  — every `Approval` row gets an `sla_deadline` (`APPROVAL_SLA_MINUTES`,
  default 60) at creation time. A plain `asyncio` background loop (not
  APScheduler/Celery — one periodic job doesn't justify a scheduling
  framework dependency) started from the FastAPI lifespan runs every
  `SLA_SWEEP_INTERVAL_SECONDS` (default 300) and escalates any
  still-`PENDING` approval past its deadline to `ESCALATED` — never
  auto-approved or auto-rejected, since a sensitive action should never
  execute or get silently blocked without a human decision. The same sweep
  flags tickets stuck in `PLANNING` for over 30 minutes (a crash/orphaned
  run past what any normal run should take). Both write an `AuditLog` entry
  under `actor="sla_sweep"` so escalations are visible in the same trail as
  every other action. Trigger a sweep pass on demand via
  `POST /admin/sla-sweep`.

Run `python -m app.db.seed` to seed both mock employees (with a
`manager_username`) and mock reviewers (`mchen`, a manager; `admin`, an
it_admin) so this is demoable out of the box — the command prints each
reviewer's token the first time it creates them (tokens aren't
re-displayable afterward; reseed against a fresh DB if you lose one).

## MCP discovery, PII masking & prompt-injection guardrails

Three gaps closed against a standard "how well do you actually know MCP"
checklist — the first is the most consequential, since it changes what MCP
discovery means for this project rather than just hardening an edge case:

- **Dynamic tool discovery, not a hardcoded reference** — the planner
  prompt's tool list used to be a hand-maintained static string
  (`TOOL_REFERENCE`) that had to be kept in sync with the real tool
  signatures by hand, with nothing checking the two didn't drift.
  `discover_tool_reference()` (`app/agent/graph.py`) now calls the live
  `tools/list` endpoint via `app/agent/mcp_client.py`'s `list_tools()` on
  every `plan_node`/`replan_node` invocation and formats whatever the
  server currently exposes — argument names, which are required vs.
  optional, and the tool's own description — directly into the prompt.
  Executor-injected args (`approval_id`, always; `ticket_id`, accepted by
  some tools but not currently populated by the executor) are filtered out
  of what's shown to the LLM, since it should never invent a value for
  either. The category-specific prompt files (`app/agent/prompts/`) became
  templates with a `{tool_reference}` placeholder, filled via
  `str.replace()` at plan time (not `str.format()` — the JSON output
  example's literal `{...}` braces would otherwise be misread as format
  placeholders). Domain-specific planning guidance that isn't expressible
  in a tool's JSON Schema (e.g. department-inference rules) still lives as
  prose in those same files — that's genuine judgment the LLM needs, not
  tool metadata a schema can carry.
- **PII masking before the LLM context window** — `identity_get_user`'s
  raw record (including `full_name` and `email`) used to be embedded whole
  into both the up-front observation (`_observe_user`) and the replan
  progress summary — on every ticket touching an existing employee, for no
  planning benefit, since neither field is referenced by any prompt or
  routing logic anywhere. `_mask_pii_for_prompt()` strips both fields
  before either call site builds its prompt; `username`, `status`,
  `department`, and `access_grants` (the fields planning logic actually
  uses) pass through unchanged. Best-effort by design: a non-JSON or
  non-dict payload (e.g. a plain `ToolError` failure string) passes through
  unmasked rather than raising, since masking must never be the reason a
  real tool result fails to reach the planner.
- **Prompt-injection framing** — ticket subject/body is the one genuinely
  untrusted input in this whole pipeline (anyone who can reach
  `POST /tickets` controls it), and it's embedded directly into four
  separate LLM calls (username extraction, classification, planning,
  replanning). `_wrap_untrusted_ticket_text()` now wraps it in explicit
  `TICKET_TEXT_START_UNTRUSTED_USER_INPUT` / `..._END` delimiters at every
  one of those call sites, paired with a matching system-prompt instruction
  (`PROMPT_INJECTION_GUARDRAIL`, threaded into all three category prompts
  plus the classify/username-extraction prompts) telling the LLM to treat
  delimited content strictly as data, never as instructions that change its
  role or output format. This is documented as a mitigation, not a
  guarantee — no prompt-level defense fully stops a determined injection.
  The real security boundary stays server-side and untouched by this: plan
  size (`MAX_PLAN_LENGTH`), username format (`_USERNAME_PATTERN`), and
  approval enforcement (`approval_gate.require_approval`) all validate the
  LLM's *output*, regardless of what convinced it to produce that output.
- **DNS-rebinding protection, per-tool rate limiting, tool annotations** —
  a follow-up pass against the MCP spec's own Transports/Tools/Security
  Best Practices pages found four more gaps, all closed: (1) the mcp SDK's
  own `TransportSecuritySettings` (Host/Origin header validation) is now
  enabled on the streamable-HTTP gateway rather than left off by default —
  see **Securing the HTTP transport for real deployment** above; (2)
  `app/mcp_server/rate_limit.py` adds a per-tool-name token bucket inside
  the gateway's own tool dispatch (`server.py`'s `_compose_gateway`),
  closing the gap where `slowapi`'s HTTP-layer limits never applied to a
  caller invoking MCP tools directly; (3) every registered tool now
  carries `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`
  annotations (e.g. `identity_disable_user` is `destructiveHint=True`,
  `identity_get_user` is `readOnlyHint=True`), propagated through the
  gateway's `add_tool()` re-registration rather than dropped; (4)
  `docker-compose.yml`'s `mcp-server` service no longer publishes its port
  to the host — only the `app` service needs to reach it, over Docker's
  internal network, and the bearer token doesn't need a second, needless
  network exposure to defend.

## Tests

```bash
pytest -v          # full suite
ruff check app/ tests/    # lint
```

264 tests (`tests/`, one file per module under test) covering, at a high level:

- **Tool layer** — CRUD + audit logging, idempotency rejection (disabling an
  already-disabled user, revoking an ungranted resource), department-based
  default access grants.
- **Security boundaries** — the approval-gate (tool/argument-mismatch
  rejection, replay prevention), per-caller API-client auth and scoping
  (`ApiClient` admin vs. standard roles, ticket/audit read scoping, daily
  request caps), reviewer-token authentication and RBAC, the MCP gateway's
  bearer-token + DNS-rebinding protections (live, against the real gateway
  app), and the target-username/ticket-text mismatch check.
- **Agent graph** — routing/guardrail logic, plan-size and username-format
  guardrails, parallel fan-out timing/correctness, dynamic replanning,
  ticket_id injection for audit attribution, PII masking before prompts,
  prompt-injection framing, node-level retry policy (fault injection
  against a real subprocess).
- **MCP layer** — dynamic tool discovery, per-tool rate limiting, per-domain
  circuit breakers, the domain-server gateway composition, the config-driven
  registry, session-reuse owner-task/queue proxy (including a live
  cross-task regression test against a real subprocess).
- **Infrastructure** — checkpointer backend selection (SQLite vs. Postgres),
  concurrent-worker startup safety, SLA timeout / stuck-ticket sweep,
  idempotency keys, structured logging, OpenTelemetry instrumentation, and
  the AG-UI streaming bridge end-to-end (a full run against a real compiled
  graph through completion/interrupt/error paths, plus the two streaming
  FastAPI endpoints' auth/DB wiring — see **Live streaming via AG-UI** above).

CI (`.github/workflows/ci.yml`) runs the full suite plus `ruff` on every push/PR
to `main`; `deploy.yml` builds and publishes the Docker image to GHCR once CI passes.

## Notes on model choice

Default is Groq's `llama-3.1-8b-instant` (free tier, fast, broadly available).
`llama-3.3-70b-versatile` is often blocked at the org level on free Groq
accounts — swap `GROQ_MODEL` in `.env` if you have access to a larger model.
To point at Anthropic, watsonx, or OpenRouter instead, set `LLM_PROVIDER`
accordingly and fill in the matching credentials in `.env` — no code changes
required.

`langchain-ibm` (and its `ibm-watsonx-ai` dependency) is a first-class,
always-installed dependency, not an optional extra — `LLM_PROVIDER=watsonx`
is ready to run today against IBM Granite (or any other model deployed on
your watsonx.ai project) once you supply real `WATSONX_API_KEY` and
`WATSONX_PROJECT_ID` values in `.env`. Note that `ChatWatsonx` authenticates
against IBM Cloud IAM eagerly at construction time (not lazily on first
call), so `get_llm()` will raise immediately on startup if the credentials
are invalid rather than failing on the first request.

**`LLM_PROVIDER=openrouter`** is a credential-free fallback for watsonx:
provisioning a watsonx.ai project on IBM Cloud's Lite plan currently requires
a credit card on file even though usage stays within the free tier, which
blocks access until that's set up. [OpenRouter](https://openrouter.ai/keys)
needs only an API key (no card) and exposes several free-tier models
(default: `meta-llama/llama-3.3-70b-instruct:free`) through an
OpenAI-compatible API — `_build_openrouter` in `app/agent/llm.py` reuses
`ChatOpenAI` pointed at OpenRouter's `base_url` rather than adding a new
SDK dependency.
