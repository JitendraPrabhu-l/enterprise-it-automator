from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: str = "groq"

    groq_api_key: str = ""
    groq_model: str = "llama-3.1-8b-instant"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"

    watsonx_api_key: str = ""
    watsonx_project_id: str = ""
    watsonx_url: str = "https://us-south.ml.cloud.ibm.com"
    watsonx_model: str = "ibm/granite-3-8b-instruct"

    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    database_url: str = "sqlite+aiosqlite:///./data/it_automator.db"
    # Either a SQLite file path (default — AsyncSqliteSaver) or a
    # "postgresql://..." connection string (AsyncPostgresSaver). Deliberately
    # a plain psycopg-style URL, not "postgresql+asyncpg://" like
    # database_url above — the two checkpointer backends use different
    # drivers (langgraph-checkpoint-postgres depends on psycopg, the app DB
    # layer on asyncpg), so this can't just reuse database_url's value even
    # when both point at the same Postgres instance.
    checkpoint_db_path: str = "./data/it_automator_checkpoints.db"

    mcp_transport: str = "stdio"
    mcp_server_url: str = "http://127.0.0.1:8765/mcp"
    mcp_server_host: str = "127.0.0.1"
    mcp_server_port: int = 8765
    # Shared secret required in the Authorization: Bearer header when the
    # MCP gateway runs over streamable-HTTP (server.py's --transport http).
    # The stdio transport is spec-exempt from needing this (it pulls
    # credentials from the environment it's spawned into), but a standalone
    # HTTP server is reachable by any network client, and the mcp SDK's
    # FastMCP applies no authentication by default — full OAuth 2.1
    # (Protected Resource Metadata, audience-scoped tokens) is ROADMAP.md
    # Stage 4.3, explicitly descoped as too large for this project; a
    # static bearer token is the right-sized fix for "zero auth at all."
    # Left blank only for local stdio-only use — main() logs a warning and
    # refuses to start the http transport without it.
    mcp_server_token: str = ""

    # Comma-separated Origin/Host allowlists for the MCP gateway's
    # streamable-HTTP transport (MCP spec 2025-11-25's "Servers MUST
    # validate the Origin header on all incoming connections to prevent
    # DNS rebinding attacks"). The installed mcp SDK already ships
    # DNS-rebinding protection (mcp.server.transport_security's
    # TransportSecuritySettings) but leaves it OFF by default "for
    # backwards compatibility" — app/mcp_server/server.py turns it on and
    # feeds it these two allowlists. Left blank by default and computed
    # from mcp_server_port at call time (see mcp_allowed_host_list below)
    # rather than a hardcoded literal, so it stays correct if the port is
    # ever changed. This project's only real caller is the agent process
    # itself (a server-to-server httpx client, which never sends an Origin
    # header at all — only browsers inject that header) — a deployment
    # fronting the gateway with a real hostname must add it here
    # explicitly rather than get a silently permissive default.
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""

    # Right-sized OAuth-2.1-flavored token exchange for the MCP HTTP
    # transport (app/mcp_server/token_exchange.py): how long a scoped,
    # domain-limited token minted from POST /token/exchange stays valid.
    # mcp_server_token above is unaffected — it remains the ADMIN
    # credential (full access, no expiry, unchanged from before this
    # existed) and the ONLY credential that can mint a scoped token in the
    # first place. Short by design: a leaked scoped token (logged, cached,
    # proxied) is both domain-limited AND time-limited, unlike the static
    # admin secret it's derived from.
    mcp_scoped_token_ttl_seconds: int = 300

    # Tool-description integrity check (app/agent/tool_integrity.py):
    # whether a mismatch between the live-discovered tool set and the
    # committed app/mcp_server/tool_baseline.json aborts the ticket (True)
    # or is only logged + counted (False, the default). False is the safe
    # default for a deployment that might legitimately ship a tool change
    # and forget to regenerate the baseline in the same PR; True is the
    # fail-closed choice for a deployment that wants an unreviewed tool
    # change to halt agent activity rather than silently keep planning
    # against it.
    tool_integrity_strict: bool = False

    # create_user/grant_access were added after a security review found they
    # ran with zero human review — unlike disable_user/revoke_access, a
    # prompt-injected or hallucinated planner output could provision access
    # for the wrong (real) employee with nobody in the loop to catch it.
    # enable_user (re-activating a previously offboarded account) carries
    # the same risk as disable_user and is gated the same way.
    # grant_app_access/revoke_app_access (app/mcp_server/app_access_server.py)
    # carry the identical risk profile as their generic grant_access/
    # revoke_access counterparts — provisioning/removing a real named SaaS
    # app (Slack, Jira, email, ...) for the wrong employee is exactly as
    # consequential as the generic-resource case, so both are gated here
    # from day one rather than repeating the "ship ungated, add gating
    # after a review finds the gap" path grant_access/create_user took.
    sensitive_actions: str = (
        "disable_user,enable_user,revoke_access,create_user,grant_access,"
        "grant_app_access,revoke_app_access"
    )

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_key: str = ""

    # Optional public, low-privilege API key auto-seeded as a STANDARD
    # ApiClient (see app/api/main.py's _ensure_demo_guest_client) and served
    # unauthenticated from GET /demo-key so a public deployment's dashboard
    # can auto-fill it for a stranger to try the app without you handing out
    # a real credential. Left blank by default (no demo client is created,
    # GET /demo-key returns null) — this is a deliberate opt-in, since it
    # means a real, working credential is served to anyone who asks for it.
    # Given a low daily_request_limit (see _ensure_demo_guest_client) since
    # it's effectively public and shares your LLM provider's quota.
    demo_api_key: str = ""

    # OTLP HTTP endpoint (e.g. a local Langfuse/Jaeger/collector instance).
    # Left blank by default — configure_observability() then leaves the
    # no-op tracer provider in place, so tracing calls throughout the app
    # stay cheap and harmless with nothing configured to receive them.
    # validation_alias pins this to the OTel-standard env var name
    # (OTEL_EXPORTER_OTLP_ENDPOINT) — pydantic-settings' default
    # auto-uppercase would otherwise look for OTEL_EXPORTER_ENDPOINT
    # instead, silently no-op'ing tracing for anyone following the
    # documented env var name in .env.example/README.md.
    otel_exporter_endpoint: str = Field(default="", validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT")

    # Stage 4.1 (right-sized): OIDC-verified reviewer identity. Enabled only
    # when BOTH issuer and audience are set (see oidc_enabled below) — with
    # them unset (the default) every auth path is exactly the pre-OIDC
    # behavior (X-Reviewer-Token only) and app/api/oidc.py is never invoked.
    # oidc_issuer must equal the token's `iss` claim exactly (e.g.
    # https://keycloak.example.com/realms/it-automator); oidc_audience must
    # appear in its `aud` claim (the client/API identifier registered in the
    # IdP). oidc_jwks_url overrides discovery for IdPs whose discovery
    # document is unreachable from this app (rare); blank means "resolve via
    # <issuer>/.well-known/openid-configuration".
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    # Which claim carries the reviewer's username to match against the
    # reviewers table. preferred_username is Keycloak's default; Auth0
    # deployments typically want "nickname" or a namespaced custom claim.
    oidc_username_claim: str = "preferred_username"

    # Hard cap on LLM tokens (input+output, summed over every call) one
    # ticket may spend across its whole lifecycle, including post-approval
    # resumes — the token-denominated companion to MAX_REPLANS's loop bound
    # (see app/agent/token_budget.py). 0 (default) disables the check
    # entirely; deployments on paid models should set an explicit ceiling
    # sized to their prompts (a normal run here is low-thousands of tokens,
    # so e.g. 50000 is roomy without being unbounded).
    max_tokens_per_ticket: int = 0

    # Org-level cost governance, beyond max_tokens_per_ticket's per-ticket
    # ceiling: a per-ApiClient and an org-wide DAILY token budget. Both
    # 0 (default) disable the respective check entirely — same opt-in
    # philosophy as max_tokens_per_ticket, since the right ceiling is
    # deployment- and pricing-specific. Enforced in two places: a
    # pre-submission check on POST /tickets (app/api/main.py, before the
    # graph even starts) and a runtime check at the same plan/replan gate
    # max_tokens_per_ticket already uses (app/agent/token_budget.py), so a
    # ticket that pushes a client/org over the cap mid-run still stops.
    max_tokens_per_client_per_day: int = 0
    max_org_tokens_per_day: int = 0

    # Stage 4.5 (scoped down): how long a sensitive approval may sit PENDING
    # before the background sweep (app/agent/sla_sweep.py) escalates it, and
    # how often that sweep runs.
    approval_sla_minutes: int = 60
    sla_sweep_interval_seconds: int = 300

    # How often the public demo client's own tickets/approvals/audit
    # entries are hard-deleted (app/agent/demo_purge.py) — "resets each
    # day" for the public demo so its tickets don't accumulate forever
    # alongside real ones. Only ever touches rows with
    # Ticket.submitted_by_client_id == the demo ApiClient's id; never runs
    # at all if DEMO_API_KEY is unset. Default 24h, not tied to
    # SLA_SWEEP_INTERVAL_SECONDS — a demo reset cadence and an approval-SLA
    # cadence are different concerns that happen to both be periodic sweeps.
    demo_data_reset_hours: int = 24

    # Optional: a Telegram bot token (from @BotFather — free, no business
    # verification needed, unlike WhatsApp's Cloud API) that lets a real
    # reviewer link their account (send /start <their reviewer token> to the
    # bot) and then get pinged with inline Approve/Reject buttons on every
    # sensitive-action approval they're entitled to decide — see
    # app/notifications/telegram.py. Left blank by default: with no token,
    # notify_reviewers_of_pending_approval() is a no-op and every existing
    # code path (dashboard-only approvals) is completely unaffected.
    # Deliberately real-reviewers-only — the public demo reviewer never gets
    # linked, so demo approval traffic never reaches anyone's personal chat.
    telegram_bot_token: str = ""

    # Verified against Telegram's own `X-Telegram-Bot-Api-Secret-Token`
    # header (set via the setWebhook API call's secret_token param) on
    # every POST /telegram/webhook request — without this, anyone who
    # discovers the webhook URL could POST a forged callback_query and
    # trigger a real approval decision, bypassing the fact that only
    # Telegram's servers are supposed to be able to reach this endpoint.
    # Left blank by default (no check) purely so local dev/testing without
    # a real Telegram webhook configured isn't forced to set this too;
    # any real deployment enabling TELEGRAM_BOT_TOKEN should also set this.
    telegram_webhook_secret: str = ""

    # Optional plain-email notifications for pending approvals (see
    # app/notifications/email.py) — a lighter-weight alternative to Telegram
    # for a reviewer who just wants a normal inbox notification, no bot/chat
    # linking. Blank smtp_host disables the whole feature: same opt-in,
    # additive, no-op-by-default shape as TELEGRAM_BOT_TOKEN above. Works
    # with any real SMTP provider (Gmail App Password, etc) — smtplib has no
    # provider-specific requirements beyond host/port/credentials.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    # Distinct from smtp_username because some providers require the
    # authenticating account to differ from the visible From address (e.g.
    # a Gmail alias) — defaults to smtp_username when left blank (see
    # Settings.smtp_from_address_or_default below).
    smtp_from_address: str = ""

    @property
    def smtp_from_address_or_default(self) -> str:
        return self.smtp_from_address or self.smtp_username

    # Voice-to-ticket: POST /tickets/transcribe (app/agent/transcription.py)
    # sends an uploaded audio clip to Groq's Whisper endpoint and returns
    # the transcript as plain text — the caller then submits that text
    # through the existing POST /tickets unchanged, so it inherits the same
    # prompt-injection framing/validation ticket text already gets, rather
    # than being a second, less-guarded way to get text into the planner.
    # No separate opt-in flag: this reuses groq_api_key (the same credential
    # the default LLM_PROVIDER=groq setup already requires), so the feature
    # is simply available whenever that's set, same as classification/
    # planning already are — a deployment on a different LLM_PROVIDER with
    # no Groq key configured gets a clear 503 (see the endpoint) rather
    # than a confusing failure deep inside a transcription call.
    stt_model: str = "whisper-large-v3-turbo"
    # 25MB matches Groq/OpenAI's own Whisper upload limit — rejecting an
    # oversized file locally (413) is a clearer failure than letting Groq's
    # API reject it after a full upload completes.
    stt_max_upload_bytes: int = 25 * 1024 * 1024

    @property
    def oidc_enabled(self) -> bool:
        """Fail-closed enablement: BOTH issuer and audience must be set.
        Issuer alone would verify tokens without an audience check — a token
        minted for any OTHER app at the same IdP would then pass here
        (the classic cross-service token-reuse hole), so half-configured
        OIDC stays OFF rather than on-but-weaker.
        """
        return bool(self.oidc_issuer and self.oidc_audience)

    @property
    def sensitive_action_set(self) -> set[str]:
        return {a.strip() for a in self.sensitive_actions.split(",") if a.strip()}

    @property
    def mcp_allowed_host_list(self) -> list[str]:
        explicit = [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]
        if explicit:
            return explicit
        return [f"127.0.0.1:{self.mcp_server_port}", f"localhost:{self.mcp_server_port}"]

    @property
    def mcp_allowed_origin_list(self) -> list[str]:
        return [o.strip() for o in self.mcp_allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
