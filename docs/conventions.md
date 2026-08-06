# Conventions

---

## Naming

| Thing | Convention | Example |
| :--- | :--- | :--- |
| Python modules, files | `snake_case` | `job_service.py` |
| Classes | `PascalCase` | `GenerationProvider` |
| Functions, variables | `snake_case` | `compute_parent_status` |
| Constants | `UPPER_SNAKE` | `MAX_RETRY_ATTEMPTS` |
| DB tables | `snake_case`, **plural** | `sub_jobs` |
| DB columns | `snake_case`, **singular** | `config_version_id` |
| DB enum types | `snake_case` + `_t` | `sub_job_status_t` |
| Enum values | `UPPER_SNAKE` | `PARTIAL_SUCCESS` |
| Timestamp columns | `<verb>_at` | `completed_at` |
| Boolean columns | `is_` / `has_` prefix | `is_active` |
| Foreign keys | `<singular_table>_id` | `job_id` |
| API routes | `kebab-case`, plural nouns | `/api/v2/jobs/{job_id}/angles/{angle}/retry` |
| JSON fields | `snake_case` | `config_version` |
| Env vars | `UPPER_SNAKE` | `SUPABASE_SERVICE_KEY` |
| Celery tasks | `<module>.<verb>_<noun>` | `matting.extract_matte` |
| Redis keys | `colon:separated:lowercase` | `idem:{client_id}:{key}` |
| Alembic migrations | `NNNN_short_description` | `0003_add_qa_columns` |

Pydantic schema classes: `<Noun><Request|Response>` — `GenerateJobRequest`,
`JobStatusResponse`. Keep API schemas in `app/api/v2/schemas/`, separate from ORM models.
Never return an ORM object from a route.

---

## Layering

```
route → service → repository → database
         └──► worker task → service → repository
```

Enforced rules:

- Routes validate input, call one service method, serialize the result. No logic.
- Services own business rules and transactions. Services do not import FastAPI.
- Repositories own every query. Nothing else writes SQL or builds a `select()`.
- Workers call services. Workers do not query directly.
- `app/providers/` is the only place that imports a model SDK.

If a function needs both a DB session and an HTTP request object, it is in the wrong layer.

---

## Error handling

**Every non-2xx response uses this envelope:**

```json
{
  "error": {
    "code": "ANGLE_NOT_ENABLED",
    "message": "Angle TOP is not enabled for category RING.",
    "details": { "category_code": "RING", "angle": "TOP" },
    "request_id": "01J..."
  }
}
```

- `code` is a stable `UPPER_SNAKE` string. The Flutter ERP branches on `code`, never on
  `message`. **Changing a code is a breaking API change.**
- `message` is safe for display to an end user. No stack traces, no internal paths, no
  provider raw errors.
- `details` is optional and structured.
- `request_id` appears on every response, success or failure, and in every log line.

**Exception hierarchy** in `app/core/errors.py`:

```
AppError                      base, carries code + http_status + message
 ├── ValidationError          422
 ├── NotFoundError            404
 ├── ConflictError            409
 ├── AuthError                401 / 403
 ├── RateLimitError           429
 └── ProviderError            carries failure_class, mapped by the worker
```

Registered handlers convert `AppError` to the envelope. An unhandled exception returns a
generic `INTERNAL_ERROR` with the `request_id`, logs the full traceback, and reports to
Sentry — **never** leaks the exception text to the client.

**In workers,** exceptions are caught at the task boundary, classified into a
`failure_class` (see @docs/business-rules.md §4), written to the sub-job, and the task
returns cleanly. A Celery task should not raise into the retry machinery unless it is a
transient class within its backoff budget.

---

## Logging

`structlog`, JSON output, stdout. No `print`. No bare `logging.getLogger` without the
structlog wrapper.

**Every log line inside a request or task carries:** `request_id`, and where applicable
`job_id`, `sub_job_id`, `angle`, `client_id`. Bind these once via context vars at the
entry point — do not pass them through every function signature.

| Level | Use for |
| :--- | :--- |
| `debug` | Local diagnosis. Off in production. |
| `info` | State transitions, job lifecycle, provider calls |
| `warning` | Degraded but handled — cache miss, Sheets fallback, transient retry |
| `error` | A sub-job failed, or a dependency is down |
| `critical` | The service cannot serve traffic |

**Never log:** API keys, `SUPABASE_SERVICE_KEY`, `GEMINI_API_KEY`, full signed URLs, raw
image bytes. Log the `key_prefix`, log the `storage_path`, never the signed URL.

---

## Database and migrations

- Every schema change is an Alembic migration. **No manual DDL in the Supabase dashboard**
  — a hand-edited schema that Alembic does not know about will be clobbered on the next
  deploy.
- Migrations are forward-only in production. Write a new migration to correct a mistake;
  do not edit a migration that has been applied.
- Every migration must have a working `downgrade()` for local development.
- Enum values are added with `ALTER TYPE ... ADD VALUE`. **Never removed** — existing rows
  reference them.
- SQLAlchemy 2.0 style throughout: `select()`, typed `Mapped[]` columns. No legacy Query API.
- Async sessions everywhere (`asyncpg`). One session per request or per task, committed or
  rolled back at the boundary.
- **Parent status recomputation and the sub-job transition that triggered it happen in the
  same transaction.** A crash between them leaves an inconsistent job.

---

## API versioning

- All routes live under `/api/v2`. The prefix is set once in the router, never repeated.
- Additive changes (new optional field, new endpoint) do not bump the version.
- Removing a field, renaming a field, or changing an error `code` **does** bump the
  version. The Flutter ERP ships on its own release cycle and cannot be updated in lockstep.

---

## Testing

- `pytest`, `pytest-asyncio`. `httpx.AsyncClient` against the app, not a live server.
- Postgres via `testcontainers`; Redis via `fakeredis`. No shared dev database.
- Celery unit tests use `task_always_eager`. Queue routing and concurrency are integration
  tests.
- Provider calls use recorded fixtures in `tests/fixtures/`. **No live API calls in CI**
  — see @docs/ai-integration.md.
- GPU tests are marked `@pytest.mark.gpu` and skipped when no card is present.
- Test names state the expectation: `test_partial_success_when_one_of_four_angles_fails`.
- Every phase checkpoint should map to a test that can be run to prove it.

---

## Configuration

- All configuration is env vars, read once into a Pydantic `Settings` object in
  `app/config.py`. **No `os.getenv` anywhere else in the codebase.**
- `.env.example` is committed and lists every variable with a comment. `.env` is gitignored.
- Secrets never appear in code, in fixtures, or in log output.
- Business configuration (prompts, thresholds, model version, pricing) lives in the
  **config version**, not in env vars. Env vars are infrastructure; config versions are
  business rules.

---

## Git

- Branches: `phase-N/short-description`.
- Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`.
- A phase is not complete until `docs/` reflects what was actually built. Documentation
  drift is a bug, and the phase self-audit checks for it explicitly.

---

## Code style

- `ruff` for lint and format. `mypy --strict` on `app/`. Both run in CI and both block merge.
- Type hints on every function signature, including tests.
- Docstrings on services and repositories explaining *why*, not *what*. The signature says
  what.
- No commented-out code on `main`. Git remembers.
