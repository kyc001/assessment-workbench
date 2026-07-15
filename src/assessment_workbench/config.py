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
    llm_model: str = "gpt-5.6-luna"
    llm_strong_model: str = "gpt-5.6-terra"
    llm_schema_in_prompt: bool = False
    llm_request_concurrency: int = 6
    exam_question_concurrency: int = 18
    exam_reviewer_attempts: int = 3
    exam_review_rounds: int = 3
    tectonic_command: str = "tectonic"
    tectonic_timeout: float = 120.0
    pdfinfo_command: str = "pdfinfo"
    pdftotext_command: str = "pdftotext"
    pdftoppm_command: str = "pdftoppm"
    pdf_inspection_timeout: float = 120.0
    pdf_raster_dpi: int = 144
