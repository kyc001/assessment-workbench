from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AW_", extra="ignore")

    workspace: Path = Path("workspaces/default")
    mineru_mode: str = "fixture"
    mineru_api_url: str = "http://127.0.0.1:8000"
    mineru_command: str = "mineru"
    http_timeout: float = 300.0
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4.1-mini"
    llm_strong_model: str = "gpt-4.1"
