from functools import lru_cache

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
    checkpoint_db_path: str = "./data/it_automator_checkpoints.db"

    mcp_transport: str = "stdio"
    mcp_server_url: str = "http://127.0.0.1:8765/mcp"
    mcp_server_host: str = "127.0.0.1"
    mcp_server_port: int = 8765

    sensitive_actions: str = "disable_user,revoke_access"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_key: str = ""

    @property
    def sensitive_action_set(self) -> set[str]:
        return {a.strip() for a in self.sensitive_actions.split(",") if a.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
