# MCP-Enabled Enterprise IT Automator

A multi-agent-style IT automation system that processes employee onboarding/offboarding
tickets by reasoning over a custom **Model Context Protocol (MCP)** server, with
**human-in-the-loop (HITL)** approval enforced server-side for sensitive actions.

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
  exposing IT-provisioning tools (`get_user`, `create_user`, `grant_access`,
  `disable_user`, `revoke_access`) over JSON-RPC, on either of two transports
  (see **MCP transport: local vs. remote** below). `create_user` auto-grants
  a default access bundle based on the employee's `department`
  (`DEPARTMENT_ACCESS_DEFAULTS` in `tools.py` — e.g. Engineering gets
  `vpn`, `github:engineering`, `jira:core-platform`; IT additionally gets
  `admin-panel`; unmapped departments get `vpn` only), so onboarding tickets
  don't need to spell out every resource. Sensitive tools
  (`disable_user`, `revoke_access`) require a server-verified `approval_id` —
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
    `FAILED` ticket state, not a crash) if the model doesn't return valid JSON.
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
curl -X POST http://127.0.0.1:8000/approvals/1/decide -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{
  "reviewer": "manager@example.com", "approve": true
}'
# -> graph resumes exactly where it paused; if the plan has more sensitive
#    steps it pauses again at the next one, otherwise the ticket completes.
```

**4. Inspect what actually happened:**

```bash
curl http://127.0.0.1:8000/tickets/1/audit -H "X-API-Key: $API_KEY"
```

## Tests

```bash
pytest -v
```

62 tests covering: tool CRUD + audit logging (including idempotency rejection
for disabling an already-disabled user / revoking an ungranted resource, and
department-based default access grants on create), the approval-gate security
boundary (tool/argument mismatch rejection, replay prevention,
unknown/pending/rejected approval refusal), the graph's routing/guardrail
logic and human-readable result summaries, the API-key auth dependency,
employee status filtering (current vs. past), MCP transport config selection,
a live streamable-HTTP round trip against a real (ephemeral) MCP server,
LLM-provider selection/error handling, and auto-creation of `data/` on a
fresh checkout with no existing DB files.

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
