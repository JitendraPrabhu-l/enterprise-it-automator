# Threat Model

STRIDE pass over the system's trust boundaries. Written for two readers: a
reviewer deciding whether the HITL guarantees are real, and future-me
changing a boundary and needing to know what it was defending against.

## System & trust boundaries

```
                        ┌─ TB1: public internet ─────────────────────────┐
  Browser / API client ─┤  FastAPI app (auth, RBAC, rate limits, UI)     │
                        └───────┬────────────────────────────────────────┘
                                │ TB2: process/network boundary (stdio or
                                │      bearer-token HTTP, no published port)
                        ┌───────▼───────────┐   ┌─ TB4: outbound ────────┐
                        │  MCP gateway      │   │ LLM provider API        │
                        │  (approval gate,  │   │ (Groq/Anthropic/...)    │
                        │   rate limit, CB) │   └─────────────────────────┘
                        └───────┬───────────┘
                                │ TB3: database (Postgres/SQLite)
                        ┌───────▼───────────┐
                        │ App DB + LangGraph│
                        │ checkpoints, audit│
                        └───────────────────┘
```

Assets, in priority order: (1) the ability to execute sensitive identity
actions, (2) the audit trail's integrity, (3) employee PII in the mock
identity store, (4) credentials (API keys, reviewer tokens, LLM keys),
(5) LLM spend.

## TB1 — Internet → API

| STRIDE | Threat | Mitigation (where) |
|---|---|---|
| S | Caller pretends to be another integration | `X-API-Key` resolved to an `ApiClient` row; no shared-string compare (`app/api/auth.py`) |
| S | Caller decides approvals as another reviewer | Per-reviewer token or OIDC-verified JWT; reviewer NEVER read from request body (`require_reviewer`) |
| T | Forged Telegram webhook decides approvals | `X-Telegram-Bot-Api-Secret-Token` verified against configured secret |
| R | "I never approved that" | Approval rows record reviewer + auth method + IdP subject + timestamps; audit log per ticket |
| I | STANDARD client reads others' tickets/audit | Caller-scoped reads (`_client_may_see_ticket`) |
| I | Non-admin exports the full cross-ticket audit log | `GET /audit/export` is ADMIN-only, unlike per-ticket audit reads; the export call itself is logged (`audit_log_exported` security event) |
| I | Clickjacking / MIME-sniffing / referrer leakage on the dashboard | `security_headers_middleware`: CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, HSTS on every response |
| I | /metrics leaks operational aggregates | Documented trade-off; block at ingress if sensitive (see `prometheus_metrics` docstring) |
| D | Request floods / LLM-spend abuse | slowapi rate limits on mutating endpoints; per-client daily caps; demo key capped at 10/day |
| E | Demo reviewer decides real approvals | Demo reviewer additionally confined to demo-owned tickets (`_authorize_demo_reviewer_scope`) |
| R | No visibility into failed-auth attempts (credential stuffing, guessed tokens) | `app/api/security_audit.py` logs invalid API keys/reviewer tokens/rejected OIDC tokens as `AuditLog` rows, queryable via `GET /audit/export` |

## TB2 — Agent → MCP gateway

| STRIDE | Threat | Mitigation |
|---|---|---|
| S | Rogue network client calls tools directly | HTTP transport requires bearer token; port not published in compose; localhost-only sidecar in Helm |
| S | DNS-rebinding from a victim's browser | Host/Origin allowlists via the SDK's TransportSecurityMiddleware, ON (off is the SDK default) |
| T/E | **Prompt-injected planner calls a sensitive tool** | The core defense stack: sensitive tools require server-verified `approval_id` for the EXACT tool+args; ticket text AND replan-time tool-call results wrapped as untrusted data with guardrail prompts (`_wrap_untrusted_ticket_text`, `_wrap_untrusted_tool_output`); plan schema/size/username validation; PII masking; injection-refusal case pinned in the golden-ticket evals |
| E | One approval reused for N executions | `executed_at` single-use marking in the approval gate |
| E | Approved args swapped at execution time | Gate matches on exact tool + arguments, mismatch refused (covered by `test_approval_target_mismatch.py`) |
| D | One backend domain down takes all calls with it | Per-domain circuit breakers + `/health` exposure + `mcp_circuit_breaker_open` metric/alert |

## TB3 — Database

| STRIDE | Threat | Mitigation |
|---|---|---|
| T | Schema drift / ad-hoc DDL | Alembic chain is the reviewed path; CI drift gate (`tests/test_migrations.py`) makes editing models without a migration fail |
| R | Audit rows silently duplicated by replicas | SLA sweep runs under a Postgres advisory lock — one pass per interval cluster-wide |
| I | Secrets in the DB (reviewer tokens, API keys) | Threat accepted at demo scope and documented: tokens are stored plaintext for inspectability; a real deployment should hash them (listed in SECURITY.md's roadmap) |
| D | Checkpointer loss orphans in-flight HITL runs | Postgres-backed checkpoints; stuck-ticket sweep fails orphans visibly instead of forever-spinners |

## TB4 — Outbound LLM calls

| STRIDE | Threat | Mitigation |
|---|---|---|
| I | Employee PII shipped to the LLM provider | `_mask_pii_for_prompt` masks emails/records before prompts; observation text is the masked form |
| D | Provider outage stalls tickets | Node-level retry policy (transient-only); failures land tickets in FAILED with reasons, not hangs |
| D (spend) | Replan loop burns tokens unboundedly | `MAX_REPLANS` + opt-in `MAX_TOKENS_PER_TICKET` hard cap + `llm_tokens_total` metrics and alert |

## OWASP Top 10 for Agentic Applications (2026) mapping

Cross-reference against the [OWASP GenAI Security Project's Agentic Top
10](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
(ASI01–ASI10), the STRIDE tables above are per-boundary; this is
per-agentic-risk-category, for a reader who came in with that checklist
specifically. "N/A" means the risk requires a capability (autonomous
multi-agent handoff, inter-agent messaging, dynamic tool/plugin install)
this single-agent, fixed-tool-surface system doesn't have — not that it
was reviewed and dismissed.

| ID | Risk | This system's posture |
|---|---|---|
| ASI01 | Agent Goal Hijack | Ticket text is the one attacker-reachable channel into planning; delimited + framed as untrusted (`_wrap_untrusted_ticket_text`); prompt-injection refusal is a pinned golden-ticket eval, not just a hope |
| ASI02 | Tool Misuse & Exploitation | Sensitive tools require a server-verified `approval_id` matching the EXACT tool+args (`approval_gate.py`) — the LLM proposing a call is never sufficient to execute it |
| ASI03 | Agent Identity & Privilege Abuse | Reviewer identity is token/OIDC-verified, never request-body-asserted (`require_reviewer`); RBAC scopes who may decide which approval (`app/api/rbac.py`) |
| ASI04 | Agentic Supply Chain Compromise | pip-audit + Trivy image scan in CI; SBOM + SLSA provenance + cosign-signed release images (`release.yml`); Dependabot |
| ASI05 | Unexpected Code Execution | No code-execution tool exists in this tool surface at all — the mock MCP tools are typed CRUD operations against a schema, not an interpreter |
| ASI06 | Memory & Context Poisoning | Both untrusted-input channels (ticket text, tool-call results fed back into `replan_node`) are delimited and framed as data-not-instructions; PII additionally stripped before either reaches the LLM |
| ASI07 | Insecure Inter-Agent Communication | N/A — single agent, no inter-agent messaging surface exists |
| ASI08 | Cascading Agent Failures | Per-domain circuit breakers isolate one MCP backend's failure from the others; node-level retry policies distinguish transient from permanent failure; `MAX_REPLANS`/`MAX_PLAN_LENGTH` bound runaway loops |
| ASI09 | Human-Agent Trust Exploitation | HITL approval is a real, server-enforced gate (not a prompt-level suggestion the LLM could talk past); Telegram/email approval channels route through the identical authorization core as the dashboard, never a weaker parallel path |
| ASI10 | Rogue Agents | Every sensitive action is individually gated and audited; there's no standing broad credential an agent (or a compromised planning step) could use beyond what one approved action authorizes |

## Known gaps (accepted, with reasons)

1. **Reviewer/API tokens stored unhashed** — inspectability for a demo
   system; flagged in SECURITY.md. Hash-at-rest is the next hardening step
   if this ever holds real identities.
2. **MCP bearer token is static** — OAuth 2.1 per MCP spec is deliberately
   descoped (ROADMAP 4.3); network isolation is the compensating control.
3. **Rate limiter is per-process** — replicas multiply effective limits;
   documented in the chart and Dockerfile; a shared store (Redis) is the
   fix when it matters.
4. **SQLite mode has no advisory locks** — single-replica by definition;
   the lock no-ops honestly rather than pretending.
