# S3 Upload Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let browser-based clients upload to S3 without a bucket CORS policy, by routing presigned uploads through the nginx that already fronts the app on port 80.

**Architecture:** `generate_upload_url` optionally rewrites the presigned URL's
scheme+host to a same-origin prefix (`S3_UPLOAD_PROXY_BASE`), keeping the path
and the entire `X-Amz-*` query string byte-identical. nginx serves that prefix
by proxying to the real S3 host with an explicit `Host` header, so the SigV4
signature — which signs only `host` plus path and query — still validates. The
browser therefore only ever talks to its own origin and CORS never applies.
nginx is a dumb pipe: it adds no credentials, so S3 still rejects any request
without a valid signature.

**Tech Stack:** FastAPI + Pydantic Settings, boto3/botocore (SigV4 presign),
nginx 1.28 on Ubuntu, pytest + moto S3 server.

**Spec:** No separate spec document. This plan implements a gap found live
during the EC2 cutover — see `docs/ec2-cutover-runbook.md` §13 and the
"Why this exists" section below, which carries the evidence.

## Why this exists

The 2026-09-09 EC2 cutover moved V2's object storage from Supabase Storage to
raw S3. Supabase Storage ships permissive CORS by default because it is built
for direct browser uploads; **an S3 bucket has no CORS rules at all unless one
is explicitly added.** So the browser upload path that worked on Render broke
on EC2 — not a regression introduced by the migration, but a gap the migration
was always going to open, invisible until a real browser upload was attempted.

Reproduced directly, against a genuinely valid presigned URL:

```
Access to fetch at 'https://image-enhancement-s3bucket.s3.amazonaws.com/...'
from origin 'http://13.203.97.235' has been blocked by CORS policy:
Response to preflight request doesn't pass access control check:
No 'Access-Control-Allow-Origin' header is present on the requested resource.
```

**Presigning does not avoid CORS.** Presigning answers "is this request
authorized"; CORS answers "may this origin be talked to at all". They are
independent, and the error above is that exact presigned URL being blocked.

The direct fix — `s3:PutBucketCORS` on the bucket — is blocked: the client's
IAM user `image-enhancement-s3-user` is denied that action, the third such
narrow-scoping wall hit during this cutover. This plan removes the dependency
on the client granting it.

**Scope note:** the production ERP is a native mobile app and is *not* subject
to CORS, so production was never blocked. This exists because the client wants
the web path working too, and because `/ui` is itself a real deliverable.

## Global Constraints

- `ruff` and `mypy --strict` on `app/` both run in CI and block merge.
- All configuration is env vars read once into `app/config.py::Settings`. **No
  `os.getenv` anywhere else** (`docs/conventions.md`).
- `tests/unit/test_deployment_docs.py::test_every_settings_field_appears_in_deployment_doc`
  requires **every** `Settings` field to appear in `docs/deployment.md`
  wrapped in backticks. Adding a setting without documenting it fails CI.
- `.env.example` is committed and lists every variable with a comment.
- Never log a full signed URL (`docs/conventions.md`, hard rule 9).

## Verified facts this plan relies on

Checked live on 2026-09-09, not assumed:

- A PUT to `image-enhancement-s3bucket.s3.amazonaws.com` with
  `redirect: 'manual'` returns **`200 OK` with no `Location` header** — the
  global endpoint serves this `ap-south-1` bucket directly. There is no 307
  to a regional endpoint for nginx to pass back to the browser.
- `ui/index.html` reads `upload_url` straight out of the presign response at
  every call site (lines 981, 1070, 1087, 1422, 1484, 1488, 1553, 1557).
  **No UI change is needed** — rewriting the URL server-side is sufficient.
- `app/api/v2/uploads.py` reads `result["signedUrl"]` at six call sites
  (lines 77, 98, 119, 132, 141, 161). That key's name must not change.

## Out of scope, deliberately

**Read/download URLs (`generate_signed_url`) are not proxied.** Result images
reach the browser through plain `<img src="...">`, which is not subject to
CORS. Only the upload `PUT` (issued via `fetch`) is blocked. If a future
browser client needs to `fetch()` image *bytes* rather than display them, that
is a separate, additive change.

## File Structure

| File | Responsibility |
| :--- | :--- |
| `app/config.py` | Declares `S3_UPLOAD_PROXY_BASE`, default `None` (feature off) |
| `app/services/storage_service.py` | Rewrites the presigned upload URL when the setting is present |
| `tests/unit/test_storage_service.py` | Proves off-by-default, rewrite correctness, and signature preservation |
| `deploy/nginx/jewelry.conf` | The nginx site config, version-controlled instead of living only on the instance |
| `docs/deployment-ec2.md` | Documents nginx as part of this deployment, and the proxy's purpose |
| `docs/deployment.md` | Secrets-checklist row (required by CI) |
| `.env.example` | The new variable, with a comment |

---

### Task 1: Config-driven upload URL rewrite

**Files:**
- Modify: `app/config.py:31` (add setting beneath `S3_ENDPOINT_URL`)
- Modify: `app/services/storage_service.py:163-180` (`generate_upload_url`)
- Modify: `.env.example` (S3 block)
- Modify: `docs/deployment.md` (secrets checklist table)
- Test: `tests/unit/test_storage_service.py`

**Interfaces:**
- Consumes: `settings.S3_ENDPOINT_URL` semantics — `None` means real AWS.
- Produces: `settings.S3_UPLOAD_PROXY_BASE: str | None`, and
  `storage_service.generate_upload_url(bucket: str, storage_path: str) -> dict[str, Any]`
  with its existing `{"signedUrl": str, "path": str}` shape unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_storage_service.py`:

```python
def test_generate_upload_url_is_unchanged_when_proxy_base_is_unset(s3: Any) -> None:
    """Default is off — the presigned URL points straight at S3."""
    result = storage_service.generate_upload_url(TEST_BUCKET, "a/b/input_1.jpg")

    assert "/s3-proxy/" not in result["signedUrl"]
    assert result["signedUrl"].startswith(settings.S3_ENDPOINT_URL or "https://")


def test_generate_upload_url_rewrites_origin_when_proxy_base_is_set(
    s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only scheme+host change. Path and the whole X-Amz-* query string are
    what SigV4 signed, so both must survive byte-identical."""
    from urllib.parse import urlsplit

    direct = storage_service.generate_upload_url(TEST_BUCKET, "a/b/input_1.jpg")["signedUrl"]
    monkeypatch.setattr(settings, "S3_UPLOAD_PROXY_BASE", "http://example.test/s3-proxy")

    proxied = storage_service.generate_upload_url(TEST_BUCKET, "a/b/input_1.jpg")["signedUrl"]

    assert proxied.startswith("http://example.test/s3-proxy/")
    assert urlsplit(proxied).path == "/s3-proxy" + urlsplit(direct).path
    assert "X-Amz-Signature=" in urlsplit(proxied).query
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in urlsplit(proxied).query


def test_generate_upload_url_proxy_base_tolerates_a_trailing_slash(
    s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator writing the env var with a trailing slash must not produce
    a double slash, which would change the signed path."""
    monkeypatch.setattr(settings, "S3_UPLOAD_PROXY_BASE", "http://example.test/s3-proxy/")

    proxied = storage_service.generate_upload_url(TEST_BUCKET, "a/b/input_1.jpg")["signedUrl"]

    assert "//a/b/input_1.jpg" not in proxied
    assert "/s3-proxy/" in proxied
```

Confirm `pytest` and `settings` are already imported at the top of that file;
add `import pytest` / `from app.config import settings` only if missing.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_storage_service.py -k proxy -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'S3_UPLOAD_PROXY_BASE'`

- [ ] **Step 3: Add the setting**

In `app/config.py`, directly below the `S3_ENDPOINT_URL` declaration (line 31):

```python
    # Same-origin base URL that presigned *upload* URLs are rewritten onto —
    # e.g. "http://13.203.97.235/s3-proxy". None (default) returns the raw S3
    # URL, which is correct for every non-browser client.
    #
    # Exists because an S3 bucket has no CORS rules unless one is explicitly
    # added, and the client's IAM user is denied s3:PutBucketCORS. A browser
    # PUT straight to S3 is blocked before it leaves the browser; routing it
    # through this API's own origin means CORS never applies. SigV4 signs
    # host + path + query only, so nginx re-sending the signed Host makes the
    # signature validate unchanged. Read URLs are deliberately NOT rewritten:
    # <img src> is not subject to CORS. See
    # docs/superpowers/plans/2026-09-09-s3-upload-proxy.md.
    S3_UPLOAD_PROXY_BASE: str | None = None
```

- [ ] **Step 4: Implement the rewrite**

In `app/services/storage_service.py`, add above `generate_upload_url`:

```python
def _apply_upload_proxy(url: str) -> str:
    """Swap the presigned URL's scheme+host for settings.S3_UPLOAD_PROXY_BASE.

    Path and query are preserved exactly — SigV4 signs the canonical path and
    the X-Amz-* query parameters, so touching either would invalidate the
    signature. The Host header is signed too, which is why the nginx side must
    re-send the original S3 host; see deploy/nginx/jewelry.conf.
    """
    base = settings.S3_UPLOAD_PROXY_BASE
    if not base:
        return url
    parts = urlsplit(url)
    proxied = f"{base.rstrip('/')}{parts.path}"
    return f"{proxied}?{parts.query}" if parts.query else proxied
```

Add `from urllib.parse import urlsplit` to the imports at the top of the file.

Then change the `return` of `generate_upload_url` (currently
`return {"signedUrl": url, "path": storage_path}`) to:

```python
    return {"signedUrl": _apply_upload_proxy(url), "path": storage_path}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_storage_service.py -v`
Expected: PASS, including the two pre-existing `generate_upload_url` tests
(`..._returns_a_signedUrl_key`, `..._round_trips_with_httpx`) — the round-trip
one proves the default-off path still uploads for real against moto.

- [ ] **Step 6: Document the setting (CI enforces this)**

In `docs/deployment.md`'s secrets checklist table, immediately after the
`S3_ENDPOINT_URL` row (line 78), add:

```markdown
| `S3_UPLOAD_PROXY_BASE` | Leave unset for every non-browser client. Set to a same-origin base URL (e.g. `http://<host>/s3-proxy`) only when a browser must upload directly — an S3 bucket has no CORS rules by default, so a browser PUT to S3 is blocked; routing it through this API's own origin avoids CORS entirely. Requires the matching nginx `location` from `deploy/nginx/jewelry.conf`. Read URLs are never rewritten — `<img src>` is not subject to CORS |
```

In `.env.example`, directly below the `S3_ENDPOINT_URL` line:

```bash
# Same-origin base for presigned UPLOAD urls, e.g. http://<host>/s3-proxy.
# Leave unset unless a browser uploads directly — see docs/deployment.md.
S3_UPLOAD_PROXY_BASE=
```

**Note:** an empty value here parses as `""`, not `None` — and `_apply_upload_proxy`
tests falsiness (`if not base`), so empty is correctly treated as off. This is
deliberate: the same empty-string-is-not-None trap took down `S3_ENDPOINT_URL`
live on 2026-09-09, because that one compares `is not None`.

- [ ] **Step 7: Run the full gate**

Run: `uv run pytest tests/unit -q && uv run ruff check app tests && uv run mypy --strict app`
Expected: all pass, including `tests/unit/test_deployment_docs.py`.

- [ ] **Step 8: Commit**

```bash
git add app/config.py app/services/storage_service.py tests/unit/test_storage_service.py docs/deployment.md .env.example
git commit -m "feat(storage): optional same-origin proxy base for presigned upload URLs"
```

---

### Task 2: Version-control the nginx config

**Files:**
- Create: `deploy/nginx/jewelry.conf`
- Modify: `docs/deployment-ec2.md`

**Interfaces:**
- Consumes: `S3_UPLOAD_PROXY_BASE` from Task 1 — the `location` path here
  (`/s3-proxy/`) must match the suffix of that setting's value.
- Produces: the deployed nginx site config, referenced by Task 3.

**Why this task exists:** nginx currently runs on the instance with a
hand-written config that exists nowhere in the repo, and nginx is not mentioned
in `docs/deployment-ec2.md` at all. A rebuilt or replaced instance would
silently lose both the routing and this proxy. This is real config drift, fixed
here rather than left latent.

- [ ] **Step 1: Create the config**

Create `deploy/nginx/jewelry.conf`:

```nginx
# Deployed to /etc/nginx/sites-available/jewelry.conf and symlinked into
# sites-enabled/. See docs/deployment-ec2.md.
#
# Port 80 is the only port the client's security group exposes — arbitrary
# ports (8000/8001) are refused by policy, so both services are routed here
# by path rather than published directly.
server {
    listen 80 default_server;
    server_name _;

    client_max_body_size 50M;

    # V2 (jewelry-api)
    location /api/v2/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }

    location /ui {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }

    # V1 (jewellery-gen-backend) — its own routes live under /api/v1, and its
    # health endpoint is at the bare /health.
    location /api/v1/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }

    location /health {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }

    # Same-origin passthrough to S3 for browser uploads. Pairs with
    # S3_UPLOAD_PROXY_BASE=http://<host>/s3-proxy.
    #
    # The trailing slash on proxy_pass strips the /s3-proxy prefix, so S3
    # receives the exact path that was signed. Host is re-sent as the real
    # bucket host because SigV4 signs it (X-Amz-SignedHeaders=host); without
    # this line the signature would not validate.
    #
    # This adds NO credentials of its own — every request still carries a
    # presigned X-Amz-Signature that S3 verifies, so an unsigned request is
    # rejected by S3 exactly as it would be without the proxy.
    location /s3-proxy/ {
        proxy_pass https://image-enhancement-s3bucket.s3.amazonaws.com/;
        proxy_set_header Host image-enhancement-s3bucket.s3.amazonaws.com;
        # S3 requires SNI for virtual-hosted bucket addressing.
        proxy_ssl_server_name on;
        # Stream the body straight through instead of buffering a whole
        # jewellery photo to disk first.
        proxy_request_buffering off;
        proxy_http_version 1.1;
        proxy_read_timeout 300s;
    }
}
```

- [ ] **Step 2: Validate the config syntax locally**

Run: `docker run --rm -v "$PWD/deploy/nginx/jewelry.conf:/etc/nginx/conf.d/jewelry.conf:ro" nginx:1.28-alpine nginx -t`

Expected: `syntax is ok` / `test is successful`. If it reports a duplicate
`default_server`, that is the stock `nginx.conf` in the image also defining
one — rerun mounting over `/etc/nginx/conf.d/default.conf` instead.

- [ ] **Step 3: Document nginx in the EC2 deployment doc**

In `docs/deployment-ec2.md`, add a section immediately after "## What runs":

```markdown
## nginx (added 2026-09-09)

The client's security group exposes **port 80 only** — they declined to open
8000/8001 ("We cannot directly expose the application on 0.0.0.0:<port>"), so
nginx fronts both services on one port and routes by path:

| Path | Upstream | Service |
| :--- | :--- | :--- |
| `/api/v2/`, `/ui` | `127.0.0.1:8000` | jewelry-api (V2) |
| `/api/v1/`, `/health` | `127.0.0.1:8001` | jewellery-gen-backend (V1) |
| `/s3-proxy/` | `image-enhancement-s3bucket.s3.amazonaws.com` | S3 upload passthrough |

The config is version-controlled at `deploy/nginx/jewelry.conf`. Install it:

```bash
sudo cp deploy/nginx/jewelry.conf /etc/nginx/sites-available/jewelry.conf
sudo ln -sf /etc/nginx/sites-available/jewelry.conf /etc/nginx/sites-enabled/jewelry.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl enable nginx   # must survive a reboot
```

**`/s3-proxy/` exists because the bucket has no CORS policy** and the client's
IAM user is denied `s3:PutBucketCORS`. Browsers block a direct PUT to S3; the
same request through this API's own origin is not cross-origin, so CORS never
applies. It pairs with `S3_UPLOAD_PROXY_BASE` — set one without the other and
uploads 404. Non-browser clients (including the production mobile ERP) are
unaffected either way. See
`docs/superpowers/plans/2026-09-09-s3-upload-proxy.md`.
```

- [ ] **Step 4: Commit**

```bash
git add deploy/nginx/jewelry.conf docs/deployment-ec2.md
git commit -m "chore(deploy): version-control the nginx site config, document the S3 upload proxy"
```

---

### Task 3: Deploy and verify a real browser upload

**Files:**
- Modify (on the instance, not in the repo): `/opt/jewelry/jewelry-api/.env`
- Modify (on the instance): `/etc/nginx/sites-available/jewelry.conf`

**Interfaces:**
- Consumes: Task 1's `S3_UPLOAD_PROXY_BASE` and Task 2's `deploy/nginx/jewelry.conf`.
- Produces: a verified browser upload path on `http://13.203.97.235`.

**Reminder for whoever runs this:** the instance is reached over **SSM**, not
SSH (`aws ssm start-session --target i-007ac3991bcadff24`), and you land as
`ssm-user` — run `sudo -iu ubuntu` first, every session.

**`<angle-bracket>` values below are placeholders you substitute.** Pasting one
literally into a shell is not a no-op — `<` is input redirection, and on
2026-09-09 exactly that wrote the literal string
`<the image-enhancement-s3-user access key>` into a live `.env`, which then
failed in a way that looked like a credentials problem.

- [ ] **Step 1: Pull the new code and install the nginx config**

```bash
cd /opt/jewelry/jewelry-api
git pull origin main
sudo cp deploy/nginx/jewelry.conf /etc/nginx/sites-available/jewelry.conf
sudo nginx -t
```

Expected: `syntax is ok` / `test is successful`, no "conflicting server name".

- [ ] **Step 2: Set the env var and reload both layers**

```bash
grep -q '^S3_UPLOAD_PROXY_BASE=' .env \
  && sed -i 's#^S3_UPLOAD_PROXY_BASE=.*#S3_UPLOAD_PROXY_BASE=http://13.203.97.235/s3-proxy#' .env \
  || echo 'S3_UPLOAD_PROXY_BASE=http://13.203.97.235/s3-proxy' >> .env
grep '^S3_UPLOAD_PROXY_BASE=' .env

sudo systemctl reload nginx
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

`--build` is not optional: this instance carried pre-existing stale images on
2026-09-09 and `up -d` alone silently reused them, so the S3 migration appeared
not to have shipped. Rebuild whenever code changed.

- [ ] **Step 3: Verify the presign response now returns a same-origin URL**

```bash
curl -s -X POST http://13.203.97.235/api/v2/uploads/presign \
  -H "X-API-Key: <a real client- or ops-scope key>" \
  -H "Content-Type: application/json" \
  -d '{"operation": "BACKGROUND_REMOVAL"}'
```

Expected: `operation_upload.upload_url` starts with
`http://13.203.97.235/s3-proxy/` and still carries `X-Amz-Signature=`.

- [ ] **Step 4: Verify the proxy actually reaches S3**

PUT a couple of bytes to that returned URL:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X PUT "<the upload_url from step 3>" \
  -H 'Content-Type: image/jpeg' --data-binary 'test'
```

Expected: `200`. A `403` means the signature broke — check the `Host` header
line in the nginx config. A `404` means the `location` path and
`S3_UPLOAD_PROXY_BASE` suffix disagree.

- [ ] **Step 5: Verify in a real browser — the check this whole plan exists for**

Open `http://13.203.97.235/ui`, enter the API key, and submit a real jewellery
photo through "Background removal / replacement".

Expected: the upload completes with **no CORS error in the browser console**,
and the job reaches a terminal status. Confirm the output image actually opens
— a `COMPLETED` status alone has previously accompanied a correctly-sized but
wrong-content file in this project, so check the bytes, not just the status.

- [ ] **Step 6: Record the result in the runbook**

Update `docs/ec2-cutover-runbook.md` §13: replace the note saying the `/ui`
job-submission check is expected to fail on CORS with what actually happened,
and cross-reference this plan.

- [ ] **Step 7: Commit**

```bash
git add docs/ec2-cutover-runbook.md
git commit -m "docs: record the verified browser upload path through the S3 proxy"
```

---

## Rollback

Set `S3_UPLOAD_PROXY_BASE=` (empty) in `.env` and re-run `up -d`. Presign
immediately returns raw S3 URLs again; the nginx `location` becomes dead but
harmless. No data migration, no schema change, nothing to undo in S3.

## Follow-ups, not part of this plan

- Ask the client for `s3:PutBucketCORS` anyway. The proxy makes it optional,
  not wrong — a real CORS policy would let browsers talk to S3 directly and
  keep upload traffic off the EC2 instance's network path.
- If a browser client ever needs to `fetch()` output image *bytes* (rather
  than display them via `<img>`), `generate_signed_url` needs the same
  treatment. Deliberately not built speculatively.
