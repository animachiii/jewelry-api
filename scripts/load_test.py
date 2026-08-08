"""Load and soak test tool for a REAL running deployment. Not a pytest test
— run by a human against a live server (local docker-compose, staging, or
production). See phases/phase-13-load-soak-tuning.md.

Burst mode (default): --concurrency virtual clients each submit --requests
jobs back to back, as fast as the server allows.

Soak mode (--soak-duration-minutes): each virtual client submits one new
job every --soak-interval-seconds for the given duration, sampling
GET /api/v2/health every minute alongside it.

Every request is a real /generate call — presign, PUT a real tiny JPEG,
POST /generate, poll GET /status until terminal. Costs real Gemini calls
against a real deployment; --dry-run prints the request plan (total
requests, estimated Redis command count) without sending anything, so you
can sanity-check against Upstash's free-tier monthly command cap first
(docs/deployment-free-tier.md, docs/capacity-tuning.md) before spending it.

Usage:
    python scripts/load_test.py --base-url http://localhost:8000 \\
        --api-key <key> --concurrency 5 --requests 10

    python scripts/load_test.py --base-url http://localhost:8000 \\
        --api-key <key> --concurrency 3 --soak-duration-minutes 30
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import statistics
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

import httpx

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]


def _real_jpeg_bytes() -> bytes:
    if Image is None:
        raise SystemExit("Pillow is required: uv sync --dev")
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color=(120, 60, 200)).save(buf, format="JPEG")
    return buf.getvalue()


@dataclass
class RequestOutcome:
    latency_seconds: float
    status_code: int
    error_code: str | None
    final_job_status: str | None


@dataclass
class Results:
    outcomes: list[RequestOutcome] = field(default_factory=list)

    def add(self, outcome: RequestOutcome) -> None:
        self.outcomes.append(outcome)

    def summarize(self) -> str:
        if not self.outcomes:
            return "No requests completed."

        latencies = sorted(o.latency_seconds for o in self.outcomes)

        def pct(p: float) -> float:
            idx = min(len(latencies) - 1, int(len(latencies) * p))
            return latencies[idx]

        status_counts = Counter(o.status_code for o in self.outcomes)
        error_counts = Counter(o.error_code for o in self.outcomes if o.error_code)
        job_status_counts = Counter(o.final_job_status for o in self.outcomes if o.final_job_status)

        lines = [
            f"Total requests:     {len(self.outcomes)}",
            f"Latency p50:        {statistics.median(latencies):.3f}s",
            f"Latency p95:        {pct(0.95):.3f}s",
            f"Latency p99:        {pct(0.99):.3f}s",
            f"Latency max:        {max(latencies):.3f}s",
            "",
            "HTTP status breakdown:",
        ]
        for code, count in sorted(status_counts.items()):
            lines.append(f"  {code}: {count}")

        if error_counts:
            lines.append("")
            lines.append("Error code breakdown (429s and others):")
            for error_code, count in error_counts.most_common():
                lines.append(f"  {error_code}: {count}")

        if job_status_counts:
            lines.append("")
            lines.append("Terminal job status breakdown:")
            for status, count in job_status_counts.most_common():
                lines.append(f"  {status}: {count}")

        return "\n".join(lines)


async def _submit_one_job(
    client: httpx.AsyncClient, api_key: str, category: str, angle: str
) -> RequestOutcome:
    start = time.monotonic()
    headers = {"X-API-Key": api_key}

    try:
        presign_resp = await client.post(
            "/api/v2/uploads/presign",
            headers=headers,
            json={"category_code": category, "angles": [angle]},
        )
        if presign_resp.status_code != 200:
            return _outcome_from_error_response(presign_resp, start)

        presigned = presign_resp.json()["angles"][0]
        put_resp = await client.put(
            presigned["upload_url"],
            content=_real_jpeg_bytes(),
            headers={"Content-Type": "image/jpeg"},
        )
        if put_resp.status_code not in (200, 201):
            return RequestOutcome(time.monotonic() - start, put_resp.status_code, None, None)

        generate_resp = await client.post(
            "/api/v2/generate",
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "category_code": category,
                "angles": {angle: {"storage_path": presigned["storage_path"]}},
            },
        )
        if generate_resp.status_code != 202:
            return _outcome_from_error_response(generate_resp, start)

        job_id = generate_resp.json()["job_id"]
        final_status = await _poll_until_terminal(client, headers, job_id)
        return RequestOutcome(time.monotonic() - start, 202, None, final_status)

    except httpx.HTTPError as exc:
        return RequestOutcome(time.monotonic() - start, 0, f"CONNECTION_ERROR:{exc!r}", None)


def _outcome_from_error_response(resp: httpx.Response, start: float) -> RequestOutcome:
    error_code = None
    with contextlib.suppress(Exception):
        error_code = resp.json().get("error", {}).get("code")
    return RequestOutcome(time.monotonic() - start, resp.status_code, error_code, None)


async def _poll_until_terminal(
    client: httpx.AsyncClient, headers: dict[str, str], job_id: str, timeout_seconds: float = 120
) -> str | None:
    deadline = time.monotonic() + timeout_seconds
    terminal = {"COMPLETED", "PARTIAL_SUCCESS", "FAILED"}
    while time.monotonic() < deadline:
        resp = await client.get(f"/api/v2/status/{job_id}", headers=headers)
        if resp.status_code != 200:
            return None
        status = resp.json()["status"]
        if status in terminal:
            return str(status)
        retry_after = float(resp.headers.get("Retry-After", "1"))
        await asyncio.sleep(retry_after)
    return "TIMEOUT"


async def _run_burst(args: argparse.Namespace, results: Results) -> None:
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
        semaphore = asyncio.Semaphore(args.concurrency)

        async def _worker() -> None:
            async with semaphore:
                outcome = await _submit_one_job(client, args.api_key, args.category, args.angle)
                results.add(outcome)

        await asyncio.gather(*(_worker() for _ in range(args.requests)))


async def _run_soak(args: argparse.Namespace, results: Results) -> None:
    end_time = time.monotonic() + args.soak_duration_minutes * 60

    async def _client_loop(client: httpx.AsyncClient) -> None:
        while time.monotonic() < end_time:
            outcome = await _submit_one_job(client, args.api_key, args.category, args.angle)
            results.add(outcome)
            await asyncio.sleep(args.soak_interval_seconds)

    async def _health_sampler(client: httpx.AsyncClient) -> None:
        while time.monotonic() < end_time:
            t0 = time.monotonic()
            try:
                resp = await client.get("/api/v2/health", headers={})
                print(
                    f"[health] t={time.monotonic():.0f} status={resp.status_code} "
                    f"latency={time.monotonic() - t0:.3f}s body={resp.text[:200]}"
                )
            except httpx.HTTPError as exc:
                print(f"[health] t={time.monotonic():.0f} ERROR {exc!r}")
            await asyncio.sleep(60)

    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
        clients = [_client_loop(client) for _ in range(args.concurrency)]
        await asyncio.gather(*clients, _health_sampler(client))


def _estimate_redis_commands(total_requests: int) -> int:
    """Rough, code-derived (not measured) per-job Redis command count — see
    docs/capacity-tuning.md for the reasoning behind this number. Counts:
    idempotency check+store (2), rate-limit incr+expire (2), Gemini
    rate-limiter incr (1), Celery broker publish + result backend writes
    (~4), status-read config cache hit (1) — ~10 as a starting estimate.
    """
    PER_JOB_ESTIMATE = 10
    return total_requests * PER_JOB_ESTIMATE


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--category", default="RING")
    parser.add_argument("--angle", default="FRONT")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--requests", type=int, default=1, help="Burst mode: total requests")
    parser.add_argument("--soak-duration-minutes", type=float, default=None)
    parser.add_argument("--soak-interval-seconds", type=float, default=30.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and estimated Redis command cost only",
    )
    args = parser.parse_args()

    soak_mode = args.soak_duration_minutes is not None

    if soak_mode:
        estimated_requests = int(
            args.concurrency * (args.soak_duration_minutes * 60 / args.soak_interval_seconds)
        )
        print(f"Soak mode: {args.concurrency} virtual clients, {args.soak_duration_minutes}min, ")
        print(f"one job every {args.soak_interval_seconds}s per client.")
    else:
        estimated_requests = args.requests
        print(f"Burst mode: {args.concurrency} concurrent clients, {args.requests} total requests.")

    print(f"Estimated total /generate calls: ~{estimated_requests}")
    print(
        f"Estimated Redis commands (rough, see docs/capacity-tuning.md): "
        f"~{_estimate_redis_commands(estimated_requests)}"
    )

    if args.dry_run:
        print("\n--dry-run: nothing sent.")
        return

    results = Results()
    if soak_mode:
        asyncio.run(_run_soak(args, results))
    else:
        asyncio.run(_run_burst(args, results))

    print("\n" + results.summarize())


if __name__ == "__main__":
    main()
