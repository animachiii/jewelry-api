"""Checkpoint 4 — /docs and /redoc render with auth applied, and are
unreachable in production. See phases/phase-1-api-contract.md Step 4.
"""

import importlib

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_docs_reachable_outside_production(path: str) -> None:
    import app.main as main_module

    importlib.reload(main_module)
    transport = ASGITransport(app=main_module.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(path)
    assert resp.status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_docs_unreachable_in_production(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "APP_ENV", "production")
    import app.main as main_module

    importlib.reload(main_module)
    transport = ASGITransport(app=main_module.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(path)
    assert resp.status_code == 404

    importlib.reload(main_module)
