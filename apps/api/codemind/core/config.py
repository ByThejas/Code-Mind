from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    """Central configuration for the CODEMIND application."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "CODEMIND"
    app_version: str = "0.1.0"
    environment: str = "development"
    debug: bool = True

    database_url: str = (
        "postgresql+psycopg://codemind:local_password_change_me@localhost:5432/codemind"
    )

    redis_url: str = "redis://localhost:6379/0"

    cors_origins: list[str] = ["http://localhost:5173"]

    # Optional read-only token. Raises the GitHub API limit from 60 to 5,000 requests/hour.
    github_token: SecretStr | None = None
    github_api_url: str = "https://api.github.com"

    # Ingestion limits (blueprint §18.3 defaults).
    max_repository_files: int = 20_000
    max_file_bytes: int = 1_000_000
    max_index_bytes: int = 500_000_000
    max_job_seconds: int = 1_800

    # A running job whose worker has not reported in for job_stale_seconds is treated as lost.
    job_heartbeat_seconds: int = 30
    job_stale_seconds: int = 300

    indexing_queue: str = "indexing"

    # Retrieval and answers (docs/retrieval-design.md). "none" keeps keyword search working without
    # a model; "ollama" uses free local models served by Ollama.
    ollama_url: str = "http://localhost:11434"
    embedding_provider: Literal["none", "ollama"] = "none"
    embedding_model: str = "bge-m3"
    embedding_dimensions: int = 1024
    embedding_batch_size: int = 64
    llm_provider: Literal["none", "ollama"] = "none"
    llm_model: str = "qwen2.5-coder:7b"
    llm_context_tokens: int = 16_384
    llm_timeout_seconds: float = 300
    answer_context_tokens: int = 8_000

    # Patch proposals (docs/patch-design.md).
    patch_max_edits: int = 20
    patch_max_files: int = 10
    patch_max_changed_lines: int = 400
    patch_context_tokens: int = 6_000


@lru_cache
def get_settings() -> Settings:
    """Return a cached application settings instance."""
    return Settings()


settings = get_settings()
