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
    # Phase 1 mock server switch — see phases/phase-1-api-contract.md Step 3.
    # Routes serve fixtures instead of real business logic. Must be false in production;
    # unimplemented routes raise instead of silently returning mock data.
    MOCK_MODE: bool = False

    # --- Supabase Postgres ---
    # Session pooler (5432), NOT transaction pooler (6543) — SQLAlchemy uses prepared statements.
    DATABASE_URL: str

    # --- Supabase Storage ---
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    BUCKET_INPUTS: str = "jewelry-inputs"
    BUCKET_OUTPUTS: str = "jewelry-outputs"
    SIGNED_URL_TTL_SECONDS: int = 3600
    # Phase 4 — Celery beat schedule for app.workers.retention.expire_assets.
    RETENTION_SWEEP_CRON: str = "0 3 * * *"

    # --- Redis ---
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    # redis-py's async client has NO socket timeout by default
    # (socket_timeout=None, socket_connect_timeout=None) -- a silently
    # stalled TCP connection (no RST, no FIN) blocks a call like
    # rate_limiter.acquire() forever. Live 2026-08-13: exactly this hung a
    # background-operation sub-job at GENERATING with zero log output,
    # wedging the entire single-worker queue (IO_QUEUE_CONCURRENCY=1) since
    # nothing else could run behind it. Same class of gap as
    # GEMINI_REQUEST_TIMEOUT_SECONDS, for the one blocking call that wasn't
    # bounded yet. Redis ops here are single-key INCR/GET/SET/EXPIRE --
    # should complete in milliseconds, so this is generous, not tight.
    REDIS_SOCKET_TIMEOUT_SECONDS: int = 5

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
    # Gemini 3.x image models are "thinking" models with no SDK default
    # timeout -- a hung call previously blocked the entire single-worker
    # queue indefinitely (IO_QUEUE_CONCURRENCY=1). Generous enough for real
    # multi-step image generation, still bounded.
    GEMINI_REQUEST_TIMEOUT_SECONDS: int = 120

    # --- Observability ---
    SENTRY_DSN: str = ""


settings = Settings()
