"""Phase 10 Checkpoint 2 — static audit that no logger call references a
secret value. Mechanizes the rule docs/conventions.md already states in
prose ("Never log: API keys, SUPABASE_SERVICE_KEY, GEMINI_API_KEY... Log the
key_prefix, log the storage_path, never the signed URL"). No server, no
fixtures — pure source-code grep.
"""

import re
import tempfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent.parent / "app"

_FORBIDDEN_TOKENS = (
    "SUPABASE_SERVICE_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "key_hash",
    "x_api_key",
)

# Matches a logger.<level>(...) call spanning one or more lines, non-greedy
# up to the matching close-paren is too fragile for nested calls — instead
# this scans each logger call's opening line plus the next few lines, which
# covers this codebase's actual call-site shape (structlog calls are short,
# see docs/conventions.md's logging section).
_LOGGER_CALL_START = re.compile(r"\blogger\.(debug|info|warning|error|critical)\s*\(")


def _find_logger_call_violations(source_files: list[Path]) -> list[str]:
    violations = []
    for path in source_files:
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if not _LOGGER_CALL_START.search(line):
                continue
            # Look at this line plus the next 5 — enough to cover any
            # logger call in this codebase without parsing full Python AST.
            window = "\n".join(lines[i : i + 6])
            depth = 0
            call_text = ""
            for ch in window:
                call_text += ch
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
            for token in _FORBIDDEN_TOKENS:
                if token in call_text:
                    violations.append(f"{path}:{i + 1}: references {token!r} in a logger call")
    return violations


def test_no_logger_call_references_a_secret() -> None:
    source_files = list(APP_ROOT.rglob("*.py"))
    assert source_files, "expected to find app/ source files"

    violations = _find_logger_call_violations(source_files)
    assert violations == [], "\n".join(violations)


def test_auth_module_never_logs() -> None:
    """app/core/auth.py handles the raw key end-to-end and must never log
    anything at all, not just avoid these specific tokens.
    """
    auth_source = (APP_ROOT / "core" / "auth.py").read_text()
    assert "logger." not in auth_source


def test_audit_catches_a_planted_violation() -> None:
    """Proves _find_logger_call_violations actually looks for the right
    thing — not a real code change, a temp file only, so this doesn't
    assert anything about the real codebase.
    """
    with tempfile.TemporaryDirectory() as tmp:
        bad_file = Path(tmp) / "bad_module.py"
        bad_file.write_text(
            "import structlog\nlogger = structlog.get_logger()\n\n"
            "def leaky() -> None:\n"
            '    logger.info("oops", key=settings.GEMINI_API_KEY)\n'
        )
        violations = _find_logger_call_violations([bad_file])
        assert len(violations) == 1
        assert "GEMINI_API_KEY" in violations[0]
