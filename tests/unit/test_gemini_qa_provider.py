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
from app.providers.gemini_qa import GeminiQaProvider

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "qa"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_high_similarity_returns_qa_result(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = _load("high_similarity.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    result = provider.score(b"output-bytes", [b"reference-bytes"])

    assert result.score == 0.94
    assert result.model_version == "gemini-2.5-flash-image-preview"


def test_low_similarity_returns_qa_result_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = _load("low_similarity.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    result = provider.score(b"output-bytes", [b"reference-bytes"])

    assert result.score == 0.41


def test_malformed_response_maps_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = _load("malformed.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    with pytest.raises(ProviderError) as exc_info:
        provider.score(b"output-bytes", [b"reference-bytes"])
    assert exc_info.value.failure_class == FailureClass.INTERNAL


def test_out_of_range_score_maps_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = {
        "candidates": [
            {
                "finish_reason": "STOP",
                "content": {"parts": [{"json": {"similarity_score": 1.5}}]},
            }
        ],
        "model_version": "gemini-2.5-flash-image-preview",
    }
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    with pytest.raises(ProviderError) as exc_info:
        provider.score(b"output-bytes", [b"reference-bytes"])
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
        provider.score(b"output-bytes", [b"reference-bytes"])
    assert exc_info.value.failure_class == FailureClass.RATE_LIMITED


def test_timeout_maps_to_transient_network(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiQaProvider(model_version="gemini-2.5-flash-image-preview")

    def _raise(*a: object, **k: object) -> None:
        raise TimeoutError("simulated ConnectTimeout")

    monkeypatch.setattr(provider, "_call_api", _raise)

    with pytest.raises(ProviderError) as exc_info:
        provider.score(b"output-bytes", [b"reference-bytes"])
    assert exc_info.value.failure_class == FailureClass.TRANSIENT_NETWORK


def test_provider_never_imports_genai_at_module_level() -> None:
    import app.providers.gemini_qa as module

    assert "google.genai" not in module.__dict__
    assert not hasattr(module, "genai")
