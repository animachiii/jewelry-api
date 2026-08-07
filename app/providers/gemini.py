"""Gemini image generation — the only module that imports `google.genai`.
See docs/conventions.md and docs/ai-integration.md Call Site 1.

`_call_api` is the real SDK call, never exercised in tests (same isolation
pattern as `app/providers/sheets.py` for Google Sheets — no real
`GEMINI_API_KEY` exists in this environment either). `generate()` is the
testable seam: tests monkeypatch `_call_api` to return a dict shaped like
`tests/fixtures/gemini/*.json`, or raise `GeminiAPIError`/`TimeoutError`,
and assert on the resulting `GenerationResult` / `ProviderError`.
"""

import base64
from typing import Any

from app.config import settings
from app.core.errors import ProviderError
from app.db.models.enums import FailureClass
from app.providers.base import GenerationProvider, GenerationResult


class GeminiAPIError(Exception):
    """A non-2xx response from the Gemini API — shaped like
    `tests/fixtures/gemini/rate_limited_429.json` / `server_error_5xx.json`.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class GeminiProvider(GenerationProvider):
    def __init__(self, model_version: str) -> None:
        self.model_version = model_version

    def _call_api(self, prompt: str, reference_images: list[bytes], seed: int) -> dict[str, Any]:
        """Real call via the google-genai SDK. Raises `GeminiAPIError` for a
        non-2xx response, `TimeoutError`/`ConnectionError` for network
        failures — `generate()` classifies both into a `ProviderError`.
        """
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        parts = [types.Part.from_text(text=prompt)]
        for image_bytes in reference_images:
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))

        try:
            response = client.models.generate_content(
                model=self.model_version,
                contents=types.Content(role="user", parts=parts),
                config=types.GenerateContentConfig(seed=seed),
            )
        except TimeoutError:
            raise
        except Exception as exc:
            status_code = getattr(exc, "code", None)
            if status_code is None:
                status_code = getattr(exc, "status_code", None)
            if not isinstance(status_code, int):
                status_code = 500
            raise GeminiAPIError(status_code, str(exc)) from exc

        return dict(response.model_dump(mode="json"))

    def generate(self, prompt: str, reference_images: list[bytes], seed: int) -> GenerationResult:
        try:
            raw = self._call_api(prompt, reference_images, seed)
        except GeminiAPIError as exc:
            raise ProviderError(
                exc.message, failure_class=self._classify_status(exc.status_code)
            ) from exc
        except TimeoutError as exc:
            raise ProviderError(
                "Gemini request timed out.", failure_class=FailureClass.TRANSIENT_NETWORK
            ) from exc
        except ConnectionError as exc:
            raise ProviderError(
                "Connection to Gemini failed.", failure_class=FailureClass.TRANSIENT_NETWORK
            ) from exc

        return self._parse_response(raw)

    @staticmethod
    def _classify_status(status_code: int) -> str:
        if status_code == 429:
            return FailureClass.RATE_LIMITED
        if 500 <= status_code < 600:
            return FailureClass.TRANSIENT_PROVIDER
        return FailureClass.INTERNAL

    def _parse_response(self, raw: dict[str, Any]) -> GenerationResult:
        candidates = raw.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderError(
                "Malformed Gemini response: no candidates.",
                failure_class=FailureClass.INTERNAL,
                details={"raw": raw},
            )

        candidate = candidates[0]
        finish_reason = candidate.get("finish_reason")

        if finish_reason == "SAFETY":
            raise ProviderError(
                "Gemini refused to generate (safety).",
                failure_class=FailureClass.SAFETY_REFUSAL,
            )

        if finish_reason != "STOP":
            raise ProviderError(
                f"Malformed Gemini response: unexpected finish_reason {finish_reason!r}.",
                failure_class=FailureClass.INTERNAL,
                details={"raw": raw},
            )

        try:
            parts = candidate["content"]["parts"]
            inline_data = parts[0]["inline_data"]
            mime_type = inline_data["mime_type"]
            image_bytes = base64.b64decode(inline_data["data"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(
                "Malformed Gemini response: missing or invalid inline image data.",
                failure_class=FailureClass.INTERNAL,
                details={"raw": raw},
            ) from exc

        model_version = raw.get("model_version", self.model_version)
        return GenerationResult(
            image_bytes=image_bytes, mime_type=mime_type, model_version=model_version
        )
