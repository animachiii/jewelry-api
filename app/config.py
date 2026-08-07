"""Pydantic Settings — the only place in the codebase that reads the environment.

See docs/conventions.md: no os.getenv anywhere else in app/.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application ---
    APP_ENV: str = "local"
    LOG_LEVEL: str = "INFO"
    API_BASE_PATH: str = "/api/v2"

    # --- Supabase Postgres ---
    # Session pooler (5432), NOT transaction pooler (6543) — SQLAlchemy uses prepared statements.
    DATABASE_URL: str

    # --- Supabase Storage ---
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    BUCKET_INPUTS: str = "jewelry-inputs"
    BUCKET_OUTPUTS: str = "jewelry-outputs"
    SIGNED_URL_TTL_SECONDS: int = 3600

    # --- Redis ---
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # --- Celery / workers ---
    IO_QUEUE_CONCURRENCY: int = 20

    # --- Models (pinned, never floating aliases) ---
    QA_MODEL_ID: str = ""

    # --- Google Sheets ---
    GOOGLE_SERVICE_ACCOUNT_JSON: str = ""
    CONFIG_SHEET_ID: str = ""
    CONFIG_SYNC_CRON: str = "*/15 * * * *"

    # --- Gemini ---
    GEMINI_API_KEY: str = ""
    GEMINI_RATE_LIMIT_PER_MINUTE: int = 60

    # --- Observability ---
    SENTRY_DSN: str = ""


settings = Settings()
