"""Phase 12 Checkpoint 3 — docs/deployment.md's secrets checklist can't
silently drift from app/config.py::Settings. No server, no fixtures.
"""

import re
import tomllib
from pathlib import Path

from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_every_settings_field_appears_in_deployment_doc() -> None:
    doc = (REPO_ROOT / "docs" / "deployment.md").read_text()
    fields = list(Settings.model_fields.keys())
    assert fields, "expected Settings to declare at least one field"

    missing = [f for f in fields if f"`{f}`" not in doc]
    assert missing == [], f"docs/deployment.md is missing: {missing}"


def test_deployment_doc_lists_no_field_that_no_longer_exists() -> None:
    doc = (REPO_ROOT / "docs" / "deployment.md").read_text()
    table_section = doc.split("## Secrets checklist")[1].split("---")[0]
    documented = set(re.findall(r"\| `([A-Z_]+)` \|", table_section))
    real_fields = set(Settings.model_fields.keys())

    stale = documented - real_fields
    assert stale == set(), f"docs/deployment.md documents fields that no longer exist: {stale}"


def test_fly_toml_files_are_valid_toml() -> None:
    for name in ("fly.toml", "fly.staging.toml"):
        with (REPO_ROOT / name).open("rb") as f:
            tomllib.load(f)


def test_fly_toml_release_command_runs_migrations() -> None:
    for name in ("fly.toml", "fly.staging.toml"):
        with (REPO_ROOT / name).open("rb") as f:
            config = tomllib.load(f)
        assert config["deploy"]["release_command"] == "alembic upgrade head"


def test_fly_toml_health_check_hits_real_health_route() -> None:
    for name in ("fly.toml", "fly.staging.toml"):
        with (REPO_ROOT / name).open("rb") as f:
            config = tomllib.load(f)
        checks = config["http_service"]["checks"]
        assert any(c["path"] == "/api/v2/health" for c in checks)


def test_workflow_yaml_files_parse() -> None:
    import yaml

    for name in ("ci.yml", "deploy.yml"):
        with (REPO_ROOT / ".github" / "workflows" / name).open() as f:
            parsed = yaml.safe_load(f)
        assert "jobs" in parsed


def test_deploy_workflow_gates_on_ci_workflow_run() -> None:
    import yaml

    with (REPO_ROOT / ".github" / "workflows" / "deploy.yml").open() as f:
        deploy = yaml.safe_load(f)
    # YAML parses the bare `on:` key as the boolean True — a real gotcha,
    # not a typo; this assertion exists specifically to catch that.
    trigger_key = True if True in deploy else "on"
    assert deploy[trigger_key]["workflow_run"]["workflows"] == ["CI"]

    for job in deploy["jobs"].values():
        assert "workflow_run.conclusion == 'success'" in job["if"]


def test_ci_workflow_triggers_on_tag_pushes() -> None:
    """deploy.yml's deploy-production job depends on workflow_run firing
    for tag pushes, which only happens if CI itself listens for them."""
    import yaml

    with (REPO_ROOT / ".github" / "workflows" / "ci.yml").open() as f:
        ci = yaml.safe_load(f)
    trigger_key = True if True in ci else "on"
    assert "v*" in ci[trigger_key]["push"]["tags"]


def test_render_yaml_is_valid_and_points_at_dockerfile() -> None:
    import yaml

    with (REPO_ROOT / "render.yaml").open() as f:
        config = yaml.safe_load(f)
    service = config["services"][0]
    assert service["dockerfilePath"] == "./Dockerfile"
    assert service["healthCheckPath"] == "/api/v2/health"
    assert service["plan"] == "free"


def test_render_start_script_runs_migrations_then_one_celery_process_and_uvicorn() -> None:
    """Free tier is 512MB total and every Python process in this container
    carries its own ~100MB interpreter+deps footprint (measured against the
    real image 2026-08-13: uvicorn 103MB, beat 47MB, worker MainProcess
    104MB, forked child 154MB once google-genai loads = ~408MB idle, before
    a job's own ~156MB peak). Four interpreters do not fit, which is what
    OOM-killed every BACKGROUND_REMOVAL run. Beat is embedded in the worker
    (-B) and the worker runs --pool=solo so there is no forked child: two
    processes total, not four.
    """
    script = (REPO_ROOT / "scripts" / "render_start.sh").read_text()
    assert "alembic upgrade head" in script
    assert "celery -A app.workers.celery_app worker" in script
    assert "uvicorn app.main:app" in script

    # Beat embedded in the worker, never its own process.
    assert "-B" in script
    assert "celery -A app.workers.celery_app beat" not in script

    # Solo pool: the worker MainProcess executes tasks itself.
    assert "--pool=solo" in script

    # prefork-only flags must not survive the switch to solo — Celery
    # ignores them there, and leaving them implies a recycling guarantee
    # that no longer exists.
    assert "--max-tasks-per-child" not in script


def test_dockerfile_default_cmd_is_render_start_script() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "./scripts/render_start.sh" in dockerfile.split("CMD")[-1]
