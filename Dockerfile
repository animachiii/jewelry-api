FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev || uv sync --no-dev

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY scripts/render_start.sh ./scripts/render_start.sh
RUN chmod +x ./scripts/render_start.sh

ENV PATH="/app/.venv/bin:$PATH"

# Default CMD runs the full stack (API + worker + beat) in one process tree
# — see scripts/render_start.sh, built for Render's free single-service
# tier (docs/deployment-free-tier.md). Fly's process groups
# (fly.toml/fly.staging.toml, docs/deployment.md) each specify their own
# command per group and never use this default.
CMD ["./scripts/render_start.sh"]
