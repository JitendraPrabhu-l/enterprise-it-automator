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
  `disable_user`, `revoke_access`) over stdio JSON-RPC. Sensitive tools
  (`disable_user`, `revoke_access`) require a server-verified `approval_id` —
  the server itself refuses the call if no human has approved that *exact*
  tool + arguments combination (`approval_gate.py`). This is a real security
  boundary, not a prompt-level suggestion the LLM could talk its way past.
- **`app/agent/`** — a [LangGraph](https://langchain-ai.github.io/langgraph/) DAG:
  `plan → route → execute_step → route → ... → finalize`, with a dedicated
  `await_approval` node that uses LangGraph's `interrupt()` to pause the whole
  graph (checkpointed) whenever the next planned step is sensitive and
  unapproved. A separate HTTP request resumes it later — this models a
  realistic multi-hour approval turnaround, not just a synchronous callback.

  - `llm.py` is a pluggable adapter — `LLM_PROVIDER=groq|anthropic|watsonx`
    swaps the backend with no code changes, so the same graph can run on a
    free Groq key today and point at IBM Granite via watsonx later.
  - The planner enforces a **structural JSON guardrail**: `_extract_json_array`
    strips markdown fences/prose and raises a clear error (routed to a
    `FAILED` ticket state, not a crash) if the model doesn't return valid JSON.
- **`app/api/`** — FastAPI endpoints to submit tickets, list/inspect them,
  list/decide pending approvals, and pull the per-ticket audit trail.
- **`app/db/`** — SQLAlchemy models: `EmployeeUser` (mock IBM ID Management
  record), `Ticket`, `Approval`, `AuditLog`. SQLite by default (zero setup);
  swap `DATABASE_URL` for Postgres in production.
- **MCP server architecture**: a real server built from scratch, registered
  and driven by an external client over the same stdio transport an
  orchestrator (watsonx Orchestrate) would use.
- **Security or enterprise auth flow**: sensitive tool calls require a
  pre-issued, single-use approval token verified server-side against tool
  name *and* arguments — prevents replay/reuse across different actions.
- **ReAct-style reasoning + guardrails**: the planner reasons step-by-step
  over the ticket, and malformed LLM output degrades to a clean `FAILED`
  ticket state instead of an unhandled exception.
- **State management in long-running workflows**: the LangGraph checkpointer
  persists agent state across the interrupt/resume boundary, so approval can
  happen minutes or hours after the ticket was submitted, from a completely
  separate HTTP request.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt

cp .env.example .env
# then set GROQ_API_KEY (free key: https://console.groq.com/keys)
```

Seed a couple of mock employees and run the API:

```bash
python -m app.db.seed
python -m uvicorn app.api.main:app --reload
```

Open `http://127.0.0.1:8000/` for the minimal web UI (submit tickets, approve/reject
pending sensitive actions, inspect the audit log), or `http://127.0.0.1:8000/docs`
for the interactive Swagger UI. Both talk to the same JSON endpoints.

The UI is a single static page (`app/static/index.html`, vanilla HTML/CSS/JS,
no build step) served directly by the FastAPI app — polls `/tickets` and
`/approvals?status=pending` every 8s and on every action.

### Run the MCP server standalone (for inspection / debugging)

```bash
python -m app.mcp_server.server
```

Speaks MCP JSON-RPC over stdio — connect with any MCP-compatible client or
the [MCP Inspector](https://github.com/modelcontextprotocol/inspector).

## Example flow

**1. Onboarding (no sensitive actions — completes immediately):**

```bash
curl -X POST http://127.0.0.1:8000/tickets -H "Content-Type: application/json" -d '{
  "requester": "hr@example.com",
  "subject": "Onboard new hire",
  "body": "Onboard Kevin Lee (username klee, email klee@example.com, dept Engineering) and grant VPN access."
}'
```

**2. Offboarding (hits the HITL gate):**

```bash
curl -X POST http://127.0.0.1:8000/tickets -H "Content-Type: application/json" -d '{
  "requester": "hr@example.com",
  "subject": "Offboard employee",
  "body": "Offboard jsmith - disable her account, she left the company."
}'
# -> response has "interrupted": true and a "pending_approval" with an approval_id
```

**3. A human reviews and decides:**

```bash
curl -X POST http://127.0.0.1:8000/approvals/1/decide -H "Content-Type: application/json" -d '{
  "reviewer": "manager@example.com", "approve": true
}'
# -> graph resumes exactly where it paused; if the plan has more sensitive
#    steps it pauses again at the next one, otherwise the ticket completes.
```

**4. Inspect what actually happened:**

```bash
curl http://127.0.0.1:8000/tickets/1/audit
```

## Tests

```bash
pytest -v
```

28 tests covering: tool CRUD + audit logging, the approval-gate security
boundary (tool/argument mismatch rejection, replay prevention, unknown/pending/
rejected approval refusal), and the graph's routing/guardrail logic.

## Notes on model choice

Default is Groq's `llama-3.1-8b-instant` (free tier, fast, broadly available).
`llama-3.3-70b-versatile` is often blocked at the org level on free Groq
accounts — swap `GROQ_MODEL` in `.env` if you have access to a larger model.
To point at Anthropic or watsonx instead, set `LLM_PROVIDER` accordingly and
fill in the matching credentials in `.env` — no code changes required.
