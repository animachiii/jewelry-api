"""FastAPI app factory. Routes and middleware registration.

See docs/api-routes.md for the full route table and docs/conventions.md for the
error envelope and logging contract every route must honor.
"""

from fastapi import FastAPI

from app.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestIDMiddleware

configure_logging()

app = FastAPI(
    title="Jewelry Generation API",
    version="2.0.0",
    description=(
        "Headless catalog imagery pipeline. Submit up to four camera angles per "
        "jewelry piece; poll for per-angle status. See docs/api-routes.md."
    ),
    docs_url="/docs" if settings.APP_ENV != "production" else None,
    redoc_url="/redoc" if settings.APP_ENV != "production" else None,
    openapi_url="/openapi.json" if settings.APP_ENV != "production" else None,
)

app.add_middleware(RequestIDMiddleware)
register_exception_handlers(app)
