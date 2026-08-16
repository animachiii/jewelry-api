"""FastAPI app factory. Routes and middleware registration.

See docs/api-routes.md for the full route table and docs/conventions.md for the
error envelope and logging contract every route must honor.
"""

from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from app.api.v2 import background as background_routes
from app.api.v2 import config as config_routes
from app.api.v2 import generate as generate_routes
from app.api.v2 import health as health_routes
from app.api.v2 import jobs as jobs_routes
from app.api.v2 import match as match_routes
from app.api.v2 import qa as qa_routes
from app.api.v2 import recolor as recolor_routes
from app.api.v2 import retry as retry_routes
from app.api.v2 import status as status_routes
from app.api.v2 import uploads as uploads_routes
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

api_v2 = APIRouter(prefix=settings.API_BASE_PATH)
api_v2.include_router(health_routes.router)
api_v2.include_router(config_routes.router)
api_v2.include_router(uploads_routes.router)
api_v2.include_router(generate_routes.router)
api_v2.include_router(background_routes.router)
api_v2.include_router(match_routes.router)
api_v2.include_router(recolor_routes.router)
api_v2.include_router(status_routes.router)
api_v2.include_router(retry_routes.router)
api_v2.include_router(jobs_routes.router)
api_v2.include_router(qa_routes.router)
app.include_router(api_v2)


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles that forces revalidation on every request.

    Starlette's StaticFiles sets `etag` and `last-modified` but no
    `Cache-Control` at all, which leaves the freshness decision entirely to
    the browser's heuristics. Safari in particular then serves a cached
    `index.html` indefinitely — observed live: a UI bug fix was deployed and
    verified present in the served bytes, while the browser kept running the
    previous build and reproducing the fixed bug.

    `no-cache` means "revalidate before use", not "don't store": the existing
    `etag` still turns each check into a cheap conditional request that
    answers 304 when nothing changed. That is the right trade for a
    single-file demo page we iterate on constantly.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


# Demo/test client only — see ui/index.html's own header and CLAUDE.md's
# "Showcase UI" note. Not the ERP integration; the Flutter team never hits
# this. Mounted same-origin because the API has no CORS setup (see
# docs/business-rules.md — v1's R21 has no v2 equivalent), so this is the
# only way a browser page can call the API at all.
app.mount("/ui", NoCacheStaticFiles(directory="ui", html=True), name="ui")

_HEALTH_PATH = f"{settings.API_BASE_PATH}/health"


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema["components"]["securitySchemes"] = {
        "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    }
    schema["security"] = [{"ApiKeyAuth": []}]
    for path, path_item in schema.get("paths", {}).items():
        if path == _HEALTH_PATH:
            for operation in path_item.values():
                operation["security"] = []

    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi  # type: ignore[method-assign]
