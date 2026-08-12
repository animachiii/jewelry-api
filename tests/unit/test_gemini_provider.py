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


def test_thought_part_before_image_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real production incident (2026-08-12): Gemini 3.x image models are
    "thinking" models whose first part is the model's text narration, not
    the image (docs/ai-integration.md Call Site 1) -- the old code assumed
    `parts[0]` was always the image and silently wrote an empty/garbage
    output to storage instead of raising."""
    provider = GeminiProvider(model_version="gemini-3.1-flash-image")
    fixture = _load("success_with_thought_part.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    result = provider.generate("a ring, front view", [], seed=42)

    assert result.mime_type == "image/png"
    assert len(result.image_bytes) > 0


def test_takes_last_inline_image_when_multiple_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Google's own docs: Gemini 3 image models generate up to two interim
    draft images before the final one, and "the last image within Thinking
    is also the final rendered image" -- must never take the first
    inline_data part found."""
    provider = GeminiProvider(model_version="gemini-3.1-flash-image")
    fixture = _load("success_with_interim_images.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    result = provider.generate("a ring, front view", [], seed=42)

    assert result.image_bytes == b"FINAL_RENDERED_IMAGE_BYTES"


def test_no_inline_image_data_maps_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response with only text parts (e.g. a refusal-shaped reply that
    still finishes with STOP) must fail loud, not silently succeed with an
    empty image."""
    provider = GeminiProvider(model_version="gemini-3.1-flash-image")
    fixture = _load("no_inline_image_data.json")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: fixture)

    with pytest.raises(ProviderError) as exc_info:
        provider.generate("prompt", [], seed=1)
    assert exc_info.value.failure_class == FailureClass.INTERNAL


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


def test_decodes_urlsafe_base64_from_the_real_sdk_serializer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixtures above are hand-written with *standard* base64, which is
    exactly why this bug reached production undetected. `model_dump(mode=
    "json")` emits **URL-safe** base64, and `base64.b64decode` silently
    mis-decodes `-`/`_` rather than erroring -- yielding a right-sized,
    entirely corrupt image. This builds the payload with the real SDK
    serializer so the alphabet is whatever google-genai/pydantic actually
    produce, not what we assumed.
    """
    from google.genai import types

    original = bytes.fromhex("ffd8ffe000104a46494600 01") + b"jewelry-bytes" * 40
    serialized = types.Blob(data=original, mime_type="image/jpeg").model_dump(mode="json")

    raw = {
        "candidates": [
            {
                "content": {"parts": [{"text": "Generating..."}, {"inline_data": serialized}]},
                "finish_reason": "STOP",
            }
        ],
        "model_version": "gemini-3.1-flash-image",
    }

    provider = GeminiProvider(model_version="gemini-3.1-flash-image")
    monkeypatch.setattr(provider, "_call_api", lambda *a, **k: raw)

    result = provider.generate("a ring", [], seed=1)

    assert result.image_bytes == original, "decoded bytes must round-trip exactly"
    assert result.image_bytes[:3] == b"\xff\xd8\xff", "must remain a valid JPEG header"


def test_provider_never_imports_genai_at_module_level() -> None:
    """docs/conventions.md: only app/providers/ imports a model SDK, and even
    there the import is deferred into _call_api so unit tests never touch it.
    """
    import app.providers.gemini as module

    assert "google.genai" not in module.__dict__
    assert not hasattr(module, "genai")
