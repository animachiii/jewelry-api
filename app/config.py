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
    # Phase 16 Step 2 — Celery beat schedule for
    # app.workers.reconciliation.sweep_stuck_sub_jobs. Deliberately frequent,
    # unlike RETENTION_SWEEP_CRON's daily cadence: a stuck job is a
    # client-visible symptom, not housekeeping — and, confirmed live
    # 2026-08-15, this free-tier instance's container restarts every 1-4
    # hours, so a once-daily cron risks being skipped entirely for a day or
    # more if the container happens to be down at the scheduled minute.
    RECONCILIATION_SWEEP_CRON: str = "*/15 * * * *"
    # Comfortably longer than WORKER_TASK_TIMEOUT_SECONDS (180s) — this
    # sweep is the backstop for jobs the timeout itself failed to catch (a
    # worker OOM-killed outright leaves no task running to hit that
    # timeout), not the primary mechanism.
    RECONCILIATION_STALE_AFTER_SECONDS: int = 600

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
    # Phase 16 Step 1 — the real enforcement behind celery_app.py's
    # task_time_limit/task_soft_time_limit, which are inert under the live
    # `--pool=solo` deployment (see that file's comment). Applied via
    # `asyncio.wait_for` around each worker task's coroutine
    # (app/workers/generation.py, app/workers/background.py) so a hang is
    # bounded regardless of which Celery pool is active.
    WORKER_TASK_TIMEOUT_SECONDS: int = 180

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

    # --- RECOLOR mask processing (Phase 19) ---
    # docs/conventions.md: tunable numeric behaviour is env-configurable,
    # not hardcoded. Pulls the mask edge back off metal/prongs a hand-drawn
    # or auto-generated mask routinely catches at its boundary — applied to
    # the overlay sent to Gemini. See app/services/mask_validation.py and
    # app/services/recolor_service.py.
    MASK_ERODE_PX: int = 2
    # Applied only to the compositing alpha (never the overlay sent to
    # Gemini, which needs a hard-edged instruction region) so the seam
    # between original and recolored pixels blends rather than showing a
    # hard ring. See recolor_service.py.
    MASK_FEATHER_PX: int = 3
    MASK_MIN_COVERAGE_PCT: float = 0.5
    MASK_MAX_COVERAGE_PCT: float = 60.0

    # --- Observability ---
    SENTRY_DSN: str = ""


settings = Settings()
