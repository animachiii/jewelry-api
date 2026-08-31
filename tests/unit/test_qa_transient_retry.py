"""qa_service._score_with_retries — bounded in-process retry around the QA
judge call, for transient provider failures only.

Regression suite for the 2026-08-30 finding: the judge call had no retry at
all while the generation call it gates has had one since Phase 6, so a
single transient Gemini 503 flagged a sub-job for human review outright
with `qa_score: NULL`. See that function's own docstring.
"""

import pytest

from app.core.errors import ProviderError
from app.db.models.enums import FailureClass
from app.providers.qa_base import QaResult
from app.services import qa_service


class _StubProvider:
    """Returns/raises a scripted sequence, one entry per call, and counts
    how many attempts actually ran. Not a GeminiQaProvider subclass on
    purpose — `_score_with_retries` only ever calls `.score()`, so the real
    class's SDK-shaped internals are irrelevant here and would only couple
    this test to them.
    """

    def __init__(self, outcomes: list[ProviderError | QaResult]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def score(self, output_image: bytes, reference_images: list[bytes], *, prompt: str) -> QaResult:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, ProviderError):
            raise outcome
        return outcome


def _transient(message: str = "503 UNAVAILABLE") -> ProviderError:
    return ProviderError(message, failure_class=FailureClass.TRANSIENT_PROVIDER)


def _ok() -> QaResult:
    return QaResult(score=0.99, model_version="test-model", reasoning="same piece")


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry's real backoff is settings-driven and irrelevant to what
    these tests assert; sleeping through it would just slow the suite.
    """
    monkeypatch.setattr(qa_service.settings, "QA_RETRY_BACKOFF_SECONDS", 0.0)


async def test_transient_failure_is_retried_and_can_then_succeed() -> None:
    """The exact live shape: one 503, then a clean score. Before this
    change the sub-job was flagged on the first 503 and never asked again.
    """
    provider = _StubProvider([_transient(), _ok()])

    result = await qa_service._score_with_retries(provider, b"out", [b"ref"], "prompt")

    assert provider.calls == 2
    assert result.score == 0.99


async def test_retries_stop_at_the_configured_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausting the budget still raises, so the caller still fails open to
    a human — this makes that the last resort, not the first response.
    """
    monkeypatch.setattr(qa_service.settings, "QA_MAX_ATTEMPTS", 3)
    provider = _StubProvider([_transient(), _transient(), _transient()])

    with pytest.raises(ProviderError) as exc_info:
        await qa_service._score_with_retries(provider, b"out", [b"ref"], "prompt")

    assert provider.calls == 3
    assert exc_info.value.failure_class == FailureClass.TRANSIENT_PROVIDER


@pytest.mark.parametrize(
    "failure_class",
    [FailureClass.INTERNAL, FailureClass.SAFETY_REFUSAL, FailureClass.INVALID_INPUT],
)
async def test_deterministic_failures_are_never_retried(failure_class: str) -> None:
    """Re-asking an identical question gets an identical answer — retrying a
    deterministic class would only delay the human handoff. A malformed
    judge response (INTERNAL) is the case that actually occurs.
    """
    provider = _StubProvider([ProviderError("nope", failure_class=failure_class), _ok()])

    with pytest.raises(ProviderError):
        await qa_service._score_with_retries(provider, b"out", [b"ref"], "prompt")

    assert provider.calls == 1


@pytest.mark.parametrize(
    "failure_class",
    [
        FailureClass.RATE_LIMITED,
        FailureClass.TRANSIENT_PROVIDER,
        FailureClass.TRANSIENT_NETWORK,
    ],
)
async def test_every_transient_class_is_retried(failure_class: str) -> None:
    """Mirrors generation_service._RETRYABLE_CLASSES exactly — the two
    constants are deliberately independent, so this pins that they agree on
    which classes are worth a second attempt.
    """
    provider = _StubProvider([ProviderError("x", failure_class=failure_class), _ok()])

    result = await qa_service._score_with_retries(provider, b"out", [b"ref"], "prompt")

    assert provider.calls == 2
    assert result.score == 0.99


async def test_a_clean_first_call_makes_exactly_one_attempt() -> None:
    """The overwhelming majority path: no retry machinery overhead when
    nothing failed.
    """
    provider = _StubProvider([_ok()])

    await qa_service._score_with_retries(provider, b"out", [b"ref"], "prompt")

    assert provider.calls == 1


# --- _resolve_judge_model ------------------------------------------------


def test_judge_model_falls_back_to_the_config_model_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset is the deployed state, so this must be a strict no-op — the
    change ships without altering any live behaviour on its own.
    """
    monkeypatch.setattr(qa_service.settings, "QA_MODEL_ID", "")

    assert qa_service._resolve_judge_model("gemini-3.1-flash-image") == "gemini-3.1-flash-image"


def test_qa_model_id_overrides_the_config_model_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/ai-integration.md's model-pinning table has always named
    QA_MODEL_ID as the judge's pinning source; until 2026-08-30 nothing read
    it, so the judge ran on the image-generation model instead.
    """
    monkeypatch.setattr(qa_service.settings, "QA_MODEL_ID", "some-text-judge-model")

    assert qa_service._resolve_judge_model("gemini-3.1-flash-image") == "some-text-judge-model"
