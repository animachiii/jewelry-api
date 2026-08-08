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

echo "Starting celery beat..."
celery -A app.workers.celery_app beat &
BEAT_PID=$!

echo "Starting celery worker..."
celery -A app.workers.celery_app worker -Q io -c "${IO_QUEUE_CONCURRENCY:-4}" --hostname=worker@%h &
WORKER_PID=$!

cleanup() {
    echo "Shutting down..."
    kill -TERM "$BEAT_PID" "$WORKER_PID" 2>/dev/null || true
    wait "$BEAT_PID" "$WORKER_PID" 2>/dev/null || true
}
trap cleanup TERM INT

echo "Starting uvicorn (foreground)..."
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" &
UVICORN_PID=$!

# If any one of the three dies, tear down the others rather than serving
# traffic with a half-dead stack (e.g. API up but no worker consuming jobs).
wait -n "$BEAT_PID" "$WORKER_PID" "$UVICORN_PID"
cleanup
exit 1
