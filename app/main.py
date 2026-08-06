"""FastAPI app factory. Routes and middleware (including the real
GET /api/v2/health from docs/api-routes.md) land in Phase 1 — this bare
instance only exists so the Phase 0 test harness and docker-compose `api`
service have something to boot against. No routes are registered here.
"""

from fastapi import FastAPI

app = FastAPI(title="jewelry-api", version="0.1.0")
