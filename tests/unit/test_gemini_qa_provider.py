"""Phase 9 Checkpoint 1 — GeminiQaProvider response classification against
tests/fixtures/qa/*.json. Never touches google.genai's network layer —
_call_api is monkeypatched in every test.
"""

import json
from pathlib import Path

import pytest

from app.core.errors import ProviderError
from app.db.models.enums import FailureClass
from app.providers.gemini import GeminiAPIError
from app.providers.gemini_qa import (
    PIECE_IDENTITY_JUDGE_PROMPT,
    SUBJECT_PRESERVATION_JUDGE_PROMPT,
    GeminiQaProvider,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "qa"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_high_similarity_returns_qa_result(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = _load("high_similarity.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    result = provider.score(
        b"output-bytes", [b"reference-bytes"], prompt=PIECE_IDENTITY_JUDGE_PROMPT
    )

    assert result.score == 0.94
    assert result.model_version == "gemini-2.5-flash-image-preview"


def test_low_similarity_returns_qa_result_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = _load("low_similarity.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    result = provider.score(
        b"output-bytes", [b"reference-bytes"], prompt=PIECE_IDENTITY_JUDGE_PROMPT
    )

    assert result.score == 0.41


def test_malformed_response_maps_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = _load("malformed.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    with pytest.raises(ProviderError) as exc_info:
        provider.score(b"output-bytes", [b"reference-bytes"], prompt=PIECE_IDENTITY_JUDGE_PROMPT)
    assert exc_info.value.failure_class == FailureClass.INTERNAL


def test_out_of_range_score_maps_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = {
        "candidates": [
            {
                "finish_reason": "STOP",
                "content": {"parts": [{"text": '{"similarity_score": 1.5}'}]},
            }
        ],
        "model_version": "gemini-2.5-flash-image-preview",
    }
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    with pytest.raises(ProviderError) as exc_info:
        provider.score(b"output-bytes", [b"reference-bytes"], prompt=PIECE_IDENTITY_JUDGE_PROMPT)
    assert exc_info.value.failure_class == FailureClass.INTERNAL


def test_rate_limited_429_maps_to_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = json.loads(
        (
            Path(__file__).resolve().parent.parent / "fixtures" / "gemini" / "rate_limited_429.json"
        ).read_text()
    )

    def _raise(*a: object, **k: object) -> None:
        raise GeminiAPIError(fixture["error"]["code"], fixture["error"]["message"])

    monkeypatch.setattr(provider, "_call_api", _raise)

    with pytest.raises(ProviderError) as exc_info:
        provider.score(b"output-bytes", [b"reference-bytes"], prompt=PIECE_IDENTITY_JUDGE_PROMPT)
    assert exc_info.value.failure_class == FailureClass.RATE_LIMITED


def test_timeout_maps_to_transient_network(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")

    def _raise(*a: object, **k: object) -> None:
        raise TimeoutError("simulated ConnectTimeout")

    monkeypatch.setattr(provider, "_call_api", _raise)

    with pytest.raises(ProviderError) as exc_info:
        provider.score(b"output-bytes", [b"reference-bytes"], prompt=PIECE_IDENTITY_JUDGE_PROMPT)
    assert exc_info.value.failure_class == FailureClass.TRANSIENT_NETWORK


def test_provider_never_imports_genai_at_module_level() -> None:
    import app.providers.gemini_qa as module

    assert "google.genai" not in module.__dict__
    assert not hasattr(module, "genai")


def test_reasoning_is_returned_on_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for the 2026-08-28 diagnostic gap: both judge prompts have
    always asked for `reasoning`, and _parse_response always threw it away,
    so a flagged output carried no explanation of *why* anywhere.
    """
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = _load("high_similarity.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    result = provider.score(
        b"output-bytes", [b"reference-bytes"], prompt=PIECE_IDENTITY_JUDGE_PROMPT
    )

    assert result.reasoning == "Matches chain style, prong count, and facet geometry."


def test_missing_reasoning_is_none_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed response that omits `reasoning` still scores — the
    reasoning is diagnostic, never load-bearing.
    """
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = {
        "candidates": [
            {
                "finish_reason": "STOP",
                "content": {"parts": [{"text": '{"similarity_score": 0.97}'}]},
            }
        ],
        "model_version": "gemini-2.5-flash-image-preview",
    }
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    result = provider.score(
        b"output-bytes", [b"reference-bytes"], prompt=PIECE_IDENTITY_JUDGE_PROMPT
    )

    assert result.score == 0.97
    assert result.reasoning is None


def test_blank_reasoning_is_normalised_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = {
        "candidates": [
            {
                "finish_reason": "STOP",
                "content": {"parts": [{"text": '{"similarity_score": 0.5, "reasoning": "   "}'}]},
            }
        ],
        "model_version": "gemini-2.5-flash-image-preview",
    }
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    assert (
        provider.score(
            b"output-bytes", [b"reference-bytes"], prompt=PIECE_IDENTITY_JUDGE_PROMPT
        ).reasoning
        is None
    )


def test_overlong_reasoning_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """`reasoning` lands in a job_events detail blob, so it is bounded."""
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = {
        "candidates": [
            {
                "finish_reason": "STOP",
                "content": {
                    "parts": [
                        {"text": json.dumps({"similarity_score": 0.5, "reasoning": "x" * 5000})}
                    ]
                },
            }
        ],
        "model_version": "gemini-2.5-flash-image-preview",
    }
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    result = provider.score(
        b"output-bytes", [b"reference-bytes"], prompt=PIECE_IDENTITY_JUDGE_PROMPT
    )

    assert result.reasoning is not None
    assert len(result.reasoning) == 500


def test_score_passes_the_caller_s_prompt_through_to_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the 2026-08-28 change: the call site chooses the
    question, and nothing in the provider substitutes a default.
    """
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    seen: list[str] = []

    def _capture(output_image: bytes, reference_images: list[bytes], prompt: str) -> dict:
        seen.append(prompt)
        return _load("high_similarity.json")

    monkeypatch.setattr(provider, "_call_api", _capture)

    provider.score(b"out", [b"ref"], prompt=SUBJECT_PRESERVATION_JUDGE_PROMPT)

    assert seen == [SUBJECT_PRESERVATION_JUDGE_PROMPT]


def test_the_two_judge_prompts_are_not_the_same_question() -> None:
    """Guards the actual regression. The background judge must tell the
    model that background/props/pose/crop differences are intended; the
    piece-identity judge must not, because for a synthetic angle they are
    not. If these ever collapse back into one string, correct
    background-operation outputs start scoring 0.0 again — see this
    module's docstring.
    """
    assert PIECE_IDENTITY_JUDGE_PROMPT != SUBJECT_PRESERVATION_JUDGE_PROMPT
    for expected in ("INTENDED", "orientation, pose", "hands", "crop"):
        assert expected in SUBJECT_PRESERVATION_JUDGE_PROMPT
        assert expected not in PIECE_IDENTITY_JUDGE_PROMPT
