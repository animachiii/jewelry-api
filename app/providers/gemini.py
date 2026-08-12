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


def _decode_b64_any_alphabet(raw: str | bytes) -> bytes:
    """Decode base64 that may use either the standard (`+/`) or URL-safe
    (`-_`) alphabet.

    `response.model_dump(mode="json")` serializes the SDK's `Blob.data` bytes
    with pydantic's **URL-safe** base64 (`_9j_4AAQ...`, where standard base64
    would be `/9j/4AAQ...`). `base64.b64decode` defaults to `validate=False`,
    so instead of rejecting `-`/`_` it silently mis-decodes them -- producing
    a byte string of exactly the right *length* but entirely wrong content.
    Live on 2026-08-12 that wrote a full-size 776KB "image/jpeg" to storage
    whose first bytes were `f63e0001` instead of JPEG's `ffd8ff`: the sub-job
    reported COMPLETED, the file downloaded fine, and it simply would not
    open. Normalizing the alphabet before decoding handles both forms, so
    this keeps working if the SDK or pydantic ever switches back.
    """
    data = raw.encode("ascii") if isinstance(raw, str) else raw
    data = data.translate(bytes.maketrans(b"-_", b"+/"))
    data += b"=" * (-len(data) % 4)  # tolerate stripped padding
    return base64.b64decode(data)


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
                config=types.GenerateContentConfig(
                    seed=seed,
                    http_options=types.HttpOptions(
                        timeout=settings.GEMINI_REQUEST_TIMEOUT_SECONDS * 1000
                    ),
                ),
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
        except (KeyError, TypeError) as exc:
            raise ProviderError(
                "Malformed Gemini response: missing or invalid inline image data.",
                failure_class=FailureClass.INTERNAL,
                details={"raw": raw},
            ) from exc

        image_bytes, mime_type = self._extract_final_image(parts)
        if image_bytes is None or mime_type is None:
            raise ProviderError(
                "Malformed Gemini response: missing or invalid inline image data.",
                failure_class=FailureClass.INTERNAL,
                details={"raw": raw},
            )

        model_version = raw.get("model_version", self.model_version)
        return GenerationResult(
            image_bytes=image_bytes, mime_type=mime_type, model_version=model_version
        )

    @staticmethod
    def _extract_final_image(parts: Any) -> tuple[bytes | None, str | None]:
        """Gemini 3.x image models are "thinking" models: `parts[0]` is
        virtually always the model's text narration, not the image
        (docs/ai-integration.md Call Site 1), and the response can carry up
        to two interim draft images before the real one. Google's own
        example code loops every part and keeps the last one with real
        `inline_data` -- "the last image within Thinking is also the final
        rendered image" (ai.google.dev/gemini-api/docs/image-generation).
        Never assume position; never accept an empty/undecodable payload as
        real image data.
        """
        if not isinstance(parts, list):
            return None, None

        image_bytes: bytes | None = None
        mime_type: str | None = None
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline_data = part.get("inline_data")
            if not isinstance(inline_data, dict):
                continue
            raw_data = inline_data.get("data")
            if not raw_data:
                continue
            try:
                decoded = _decode_b64_any_alphabet(raw_data)
            except (TypeError, ValueError):
                continue
            if not decoded:
                continue
            image_bytes = decoded
            mime_type = inline_data.get("mime_type")

        return image_bytes, mime_type
