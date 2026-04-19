"""Application configuration via environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """OpenBoek configuration — reads from .env or environment."""

    # Database
    database_url: str = "postgresql+asyncpg://openboek:openboek@localhost:5432/openboek"

    # Security
    secret_key: str = "change-me-in-production"

    # AI / Ollama
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "gemma4"

    # Application
    app_lang: str = "nl"
    app_port: int = 8070
    app_host: str = "0.0.0.0"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
