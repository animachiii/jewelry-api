"""The /ui mount must force revalidation.

Regression guard for a real incident: a UI fix was deployed and verified
present in the served bytes, while Safari kept running the previous build and
reproducing the already-fixed bug. Starlette's StaticFiles sets etag and
last-modified but no Cache-Control, leaving freshness to browser heuristics.
"""

from starlette.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_ui_is_served_with_revalidation_headers() -> None:
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache, must-revalidate"


def test_ui_still_answers_304_so_revalidation_stays_cheap() -> None:
    """`no-cache` means "revalidate", not "re-download" — the etag must still
    produce a conditional 304, or this trades a stale-cache bug for a
    bandwidth one.
    """
    first = client.get("/ui/")
    etag = first.headers["etag"]

    second = client.get("/ui/", headers={"If-None-Match": etag})
    assert second.status_code == 304


def test_api_routes_are_not_given_cache_headers() -> None:
    """The subclass must only affect the static mount."""
    resp = client.get("/api/v2/health")
    assert resp.status_code == 200
    assert "cache-control" not in resp.headers
