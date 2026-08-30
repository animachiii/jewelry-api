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
    # 2026-08-28 — every Supabase Storage call goes through
    # storage_service._with_retries. A transient httpx.TransportError (a real
    # network blip, never a real HTTP error response from Supabase, which
    # raises storage3.StorageException instead and is never retried) was
    # 500-ing real requests in production and, more visibly, failing CI
    # nondeterministically on unrelated PRs -- five times in one week, always
    # a different test, always this same signature. See
    # storage_service.py's own module docstring.
    STORAGE_MAX_ATTEMPTS: int = 3
    STORAGE_RETRY_BACKOFF_SECONDS: float = 0.5
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

    # MIX_SEAM_BAND_PX was removed 2026-08-31. It sized the ring drawn around
    # the graft boundary after MIX's deterministic rough-composite step; MIX no
    # longer grafts anything, so there is no seam to band. The contour width on
    # its new highlight images is mix_service._HIGHLIGHT_OUTLINE_PX, a module
    # constant rather than a setting — it is a fixed property of how the two
    # reference images are drawn, not something an operator would ever tune per
    # deployment the way a memory ceiling is. See app/services/mix_service.py's
    # module docstring and docs/business-rules.md §16.

    # --- Image processing memory ceiling (post-Phase-20 incident) ---
    # Live 2026-08-24 on the free-tier jewelry-api Render service: a single
    # RECOLOR request pushed memory from a ~200MB baseline to 487MB (91% of
    # the 512MB limit) in one sample, OOM-killing the container. Root cause:
    # RECOLOR's overlay-building step (app/services/recolor_service.py::
    # _build_overlay) and MIX's seam-overlay step (app/services/mix_service.py
    # ::_build_seam_overlay) decoded the client's full-resolution upload —
    # a real jewelry photo can be 4000px+ on the long edge, ~36MB per
    # decoded RGB buffer — and held several such buffers simultaneously
    # (source, mask, eroded mask, magenta fill, composite result). Neither
    # RECOLOR's nor MIX's own compositing correctness needs the image sent
    # to Gemini at full resolution — only generate-then-composite's *final*
    # step does, and that step already stays at full resolution unmodified.
    # This cap bounds the pre-provider-call overlay-building path in both
    # services. **As of 2026-08-27 it also bounds MIX's entire working canvas**
    # (app/services/mix_service.py::_load_downscaled), so a MIX job's
    # client-facing output is capped at this edge rather than the primary
    # photo's native size. The two earlier, narrower passes (2026-08-24,
    # 2026-08-25) were both measured insufficient on a real 12.6MP upload:
    # ~187MB needed against ~160MB of headroom, OOM-killed before every single
    # Gemini attempt. Capping the canvas took that to ~74MB.
    # Raising this value therefore raises MIX's output resolution AND its
    # memory ceiling together — do not raise it without re-measuring against
    # the instance's real baseline. RECOLOR's *final* composite is deliberately
    # NOT bounded by this (its guarantee is stated against the untouched
    # original source) and still carries the same exposure on a large upload —
    # see docs/business-rules.md §15/§16.
    WORKING_MAX_EDGE: int = 2048

    # --- Observability ---
    SENTRY_DSN: str = ""


settings = Settings()
