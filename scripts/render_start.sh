#!/usr/bin/env bash
# Render's free Web Service tier has no free Background Worker tier and no
# free managed Redis — see docs/deployment-free-tier.md. This script runs
# the Celery worker and beat as ordinary background OS processes inside the
# SAME container as the API, so the whole stack fits in Render's one free
# service. Render bills per *service*, not per process inside a container's
# process tree — this is a process-supervision fix at the shell level, not
# an application-code change. No app/config.py flag needed, unlike V1's
# WORKER_IN_PROCESS (which ran the worker loop as an asyncio task inside the
# same Python process for a different runtime — ARQ vs Celery here).
#
# `alembic upgrade head` runs once before any process starts — Render's free
# tier has no separate release-command hook the way Fly's [deploy] does, so
# this script is also the migration-on-deploy mechanism for this path.
set -euo pipefail

echo "Running migrations..."
alembic upgrade head

echo "Starting celery worker (solo pool, embedded beat)..."
# ONE celery process, not three. Measured against this exact image on
# 2026-08-13, every Python process here costs ~100MB of interpreter+deps
# before doing any work:
#
#   uvicorn                                    103 MB
#   celery beat (own process)                   47 MB
#   celery worker MainProcess                  104 MB
#   celery worker forked child (+google-genai) 154 MB
#   ------------------------------------------------
#   idle total                                ~408 MB   of a 512MB tier
#
# and one BACKGROUND_REMOVAL job peaks at ~156MB inside the child (a 4MB
# phone photo, measured through the real google-genai serialization path:
# request base64, then a thinking-model response carrying two interim draft
# images plus the final one, then the QA call sending input+output up
# again). 408 + a job does not fit in 512, which is what OOM-killed every
# background-operation run on 2026-08-13 -- not the per-copy waste trimmed
# earlier that day, which was real but an order of magnitude too small.
#
# --pool=solo: the MainProcess runs tasks itself, so there is no forked
# child at all. Costs nothing in throughput -- IO_QUEUE_CONCURRENCY was
# already 1, so nothing ran in parallel anyway. What it does give up is
# prefork's isolation: there is no child to recycle, so the per-child task
# ceiling this script used to pass (a prefork-only flag, deliberately
# dropped) no longer applies and the deferred google-genai import stays
# resident for the process's life. That is a bounded ~50MB
# one-time growth, not an unbounded leak, and it is a far better trade than
# being OOM-killed on every job. A task that hangs now blocks the queue --
# bounded by GEMINI_REQUEST_TIMEOUT_SECONDS and REDIS_SOCKET_TIMEOUT_SECONDS
# (PR #16), which is why those had to land first.
#
# -B: beat runs as a thread inside the worker instead of its own process.
# Celery warns against embedded beat for production, and that warning is
# about multi-worker deployments where several embedded schedulers would
# double-fire tasks. There is exactly one worker here, by construction (one
# free instance), and one hourly task -- CONFIG_SYNC_CRON.
celery -A app.workers.celery_app worker -Q io -B --pool=solo --hostname=worker@%h &
WORKER_PID=$!

cleanup() {
    echo "Shutting down..."
    kill -TERM "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
}
trap cleanup TERM INT

echo "Starting uvicorn (foreground)..."
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" &
UVICORN_PID=$!

# If either dies, tear down the other rather than serving traffic with a
# half-dead stack (e.g. API up but no worker consuming jobs). Beat is no
# longer named here — it is a thread inside $WORKER_PID now, so it dies
# with it and cannot be waited on separately (naming it under `set -u`
# would abort this script outright).
wait -n "$WORKER_PID" "$UVICORN_PID"
cleanup
exit 1
