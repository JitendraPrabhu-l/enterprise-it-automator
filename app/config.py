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

    database_url: str = "sqlite+aiosqlite:///./it_automator.db"

    sensitive_actions: str = "disable_user,revoke_access"

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    @property
    def sensitive_action_set(self) -> set[str]:
        return {a.strip() for a in self.sensitive_actions.split(",") if a.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
