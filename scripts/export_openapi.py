"""Exports the live OpenAPI 3.1 spec to docs/openapi.json.

python scripts/export_openapi.py

CI runs this and diffs the result against the committed file — see
phases/phase-1-api-contract.md Checkpoint 2.
"""

import json
from pathlib import Path

from app.main import app

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def main() -> None:
    schema = app.openapi()
    OUTPUT_PATH.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
