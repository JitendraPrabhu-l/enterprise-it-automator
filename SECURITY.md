# Security Policy

## Reporting a vulnerability

Email **jitendraprabhugrl@gmail.com** with reproduction steps. Please do not
open a public issue for anything exploitable; you'll get an acknowledgement
within a few days and credit in the fix's release notes if you want it.
There is no bug bounty — this is a solo-maintained project.

## Security model in one page

The full analysis lives in [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md);
these are the load-bearing properties, each enforced server-side and covered
by tests:

- **Human-in-the-loop is a server boundary, not a prompt suggestion.**
  Sensitive MCP tools (`disable_user`, `enable_user`, `create_user`,
  `grant_access`, `revoke_access`) refuse to execute without a
  server-verified `approval_id` matching the *exact* tool + arguments a
  human approved (`app/mcp_server/approval_gate.py`). One approval
  authorizes one execution (`executed_at` single-use marking).
- **Reviewer identity is authenticated, not asserted.** Deciding an
  approval requires a per-reviewer secret (`X-Reviewer-Token`) or, when
  OIDC is configured, an IdP-verified JWT (issuer + audience + signature +
  expiry, RS256 pinned — `app/api/oidc.py`). The decision records HOW it
  was authenticated (`reviewer_auth_method`, `reviewer_oidc_subject`).
- **Authorization is role/relationship-scoped** (`app/api/rbac.py`):
  `it_admin` decides anything; a `manager` only approvals targeting their
  own reports; the public demo reviewer only demo-owned tickets.
- **Ticket text is treated as untrusted input**: wrapped in delimiters with
  an explicit injection guardrail prompt, planner output is
  schema-validated (JSON array, plan-size cap, username format check), PII
  is masked before employee records enter any LLM prompt, and the
  golden-ticket eval suite pins an injection-refusal case in CI.
- **The MCP gateway's HTTP transport requires a bearer token** and enforces
  DNS-rebinding Host/Origin validation; compose never publishes its port;
  the Helm chart runs it as a localhost-only sidecar.
- **API access is keyed and scoped**: every data endpoint requires
  `X-API-Key` resolved to an `ApiClient` row; STANDARD clients only see
  tickets they filed; mutating endpoints are rate-limited; per-client daily
  request caps bound the public demo key.
- **Cost containment**: `MAX_TOKENS_PER_TICKET` (opt-in) hard-caps LLM
  spend per ticket, replans are bounded by `MAX_REPLANS`.

## Supply chain & operations

- Dependencies are locked (`requirements.lock.txt`, uv-compiled) and
  audited in CI (`pip-audit`, strict); Dependabot files weekly grouped
  update PRs that must pass the full gate (ruff, mypy, pytest+coverage).
- Images are Trivy-scanned (fail on fixable HIGH/CRITICAL) *before* being
  pushed to GHCR; releases are semver-tagged images built from the tag.
- Tagged releases are keylessly signed with cosign (GitHub OIDC → Sigstore,
  no stored signing key) and carry a signed SBOM (SPDX) and SLSA provenance
  attestation, both keyed to the immutable push digest — verify with
  `cosign verify`/`cosign verify-attestation` against
  `ghcr.io/<repo>@<digest>` (see `.github/workflows/release.yml`'s
  comments for the exact identity/issuer to check against).
- Containers run as a non-root user; the Helm chart drops all capabilities
  and sets `runAsNonRoot`/seccomp defaults.
- Secrets are env-injected (never baked into images or committed);
  `.dockerignore`/`.gitignore` exclude `.env`; compose refuses to start
  with unset required secrets rather than falling back to defaults.

## Out of scope (documented, deliberate)

- Full OAuth 2.1 between MCP client and gateway (static bearer + network
  isolation instead — ROADMAP 4.3).
- SCIM/LDAP identity sync (mock identity store is the point of the demo —
  ROADMAP 4.4).
- Multi-tenancy: one deployment = one trust domain.
