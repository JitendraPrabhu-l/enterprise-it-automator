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

    # create_user/grant_access were added after a security review found they
    # ran with zero human review — unlike disable_user/revoke_access, a
    # prompt-injected or hallucinated planner output could provision access
    # for the wrong (real) employee with nobody in the loop to catch it.
    # enable_user (re-activating a previously offboarded account) carries
    # the same risk as disable_user and is gated the same way.
    sensitive_actions: str = "disable_user,enable_user,revoke_access,create_user,grant_access"

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
