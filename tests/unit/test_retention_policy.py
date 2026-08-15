"""Phase 16 Step 4 Checkpoint — OUTPUT retention is a real, finite value,
not the indefinite placeholder it started as. See
app/services/retention_policy.py's module docstring and
docs/storage-audit-2026-08.md for why 180 was chosen.
"""

from datetime import UTC, datetime, timedelta

from app.db.models.enums import AssetKind
from app.services.retention_policy import RETENTION_DAYS, compute_expires_at


def test_output_retention_is_a_real_finite_value() -> None:
    assert RETENTION_DAYS[AssetKind.OUTPUT] == 180


def test_input_and_matte_retention_unchanged() -> None:
    assert RETENTION_DAYS[AssetKind.INPUT] == 90
    assert RETENTION_DAYS[AssetKind.MATTE] == 30


def test_compute_expires_at_returns_a_deadline_for_output_now() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    expires_at = compute_expires_at(AssetKind.OUTPUT, now=now)
    assert expires_at == now + timedelta(days=180)
