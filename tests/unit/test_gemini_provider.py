"""Phase 6 Checkpoint 2 — GeminiProvider response classification against
tests/fixtures/gemini/*.json. Never touches google.genai's network layer —
_call_api is monkeypatched in every test.
"""

import json
from pathlib import Path

import pytest

from app.core.errors import ProviderError
from app.db.models.enums import FailureClass
from app.providers.gemini import GeminiAPIError, GeminiProvider

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gemini"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_success_returns_generation_result(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = _load("success.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    result = provider.generate("a ring, front view", [], seed=42)

    assert result.mime_type == "image/png"
    assert len(result.image_bytes) > 0
    assert result.model_version == "gemini-2.5-flash-image-preview"


def test_rate_limited_429_maps_to_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = _load("rate_limited_429.json")

    def _raise(*a: object, **k: object) -> None:
        raise GeminiAPIError(fixture["error"]["code"], fixture["error"]["message"])

    monkeypatch.setattr(provider, "_call_api", _raise)

    with pytest.raises(ProviderError) as exc_info:
        provider.generate("prompt", [], seed=1)
    assert exc_info.value.failure_class == FailureClass.RATE_LIMITED


def test_server_5xx_maps_to_transient_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = _load("server_error_5xx.json")

    def _raise(*a: object, **k: object) -> None:
        raise GeminiAPIError(fixture["error"]["code"], fixture["error"]["message"])

    monkeypatch.setattr(provider, "_call_api", _raise)

    with pytest.raises(ProviderError) as exc_info:
        provider.generate("prompt", [], seed=1)
    assert exc_info.value.failure_class == FailureClass.TRANSIENT_PROVIDER


def test_safety_refusal_maps_to_safety_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = _load("safety_refusal.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    with pytest.raises(ProviderError) as exc_info:
        provider.generate("prompt", [], seed=1)
    assert exc_info.value.failure_class == FailureClass.SAFETY_REFUSAL


def test_malformed_response_maps_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiProvider(model_version="gemini-2.5-flash-image-preview")
    fixture = _load("malformed_response.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    with pytest.raises(ProviderError) as exc_info:
        provider.generate("prompt", [], seed=1)
    assert exc_info.value.failure_class == FailureClass.INTERNAL


def test_timeout_maps_to_transient_network(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GeminiProvider(model_version="gemini-2.5-flash-image-preview")

    def _raise(*a: object, **k: object) -> None:
        raise TimeoutError("simulated ConnectTimeout")

    monkeypatch.setattr(provider, "_call_api", _raise)

    with pytest.raises(ProviderError) as exc_info:
        provider.generate("prompt", [], seed=1)
    assert exc_info.value.failure_class == FailureClass.TRANSIENT_NETWORK


def test_provider_never_imports_genai_at_module_level() -> None:
    """docs/conventions.md: only app/providers/ imports a model SDK, and even
    there the import is deferred into _call_api so unit tests never touch it.
    """
    import app.providers.gemini as module

    assert "google.genai" not in module.__dict__
    assert not hasattr(module, "genai")
