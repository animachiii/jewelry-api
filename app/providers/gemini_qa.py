"""Gemini-judged similarity scoring — the QA implementation of QaProvider.
See docs/conventions.md and docs/ai-integration.md Call Site 2.

`_call_api` is the real SDK call, never exercised in tests (no real
GEMINI_API_KEY exists in this environment — same isolation pattern as
app/providers/gemini.py). `score()` is the testable seam: tests monkeypatch
`_call_api` to return a dict shaped like tests/fixtures/qa/*.json, or raise
GeminiAPIError/TimeoutError, and assert on the resulting QaResult /
ProviderError.

**Found live 2026-08-21, same bug class as the base64 image-decoding bug**:
`_parse_response` originally read `parts[0]["json"]`, a key the real SDK
never produces — `google.genai.types.Part` has no `json` field, only `text`
(a plain string) or `inline_data` (bytes). Every real background-operation
QA call was silently failing with `qa_provider_error`/`INTERNAL` and
fail-open-ing to `QA_REVIEW`/`FLAGGED`, `qa_score: None` — every job, not
some. `tests/fixtures/qa/*.json` was hand-written against the same wrong
assumption and passed CI the whole time, because `_call_api` is monkeypatched
in every test and the real SDK never actually ran. Fixed by requesting
`response_mime_type="application/json"` and parsing `parts[0]["text"]` as a
JSON string.
"""

import json
from typing import Any

from app.config import settings
from app.core.errors import ProviderError
from app.db.models.enums import FailureClass
from app.providers.gemini import GeminiAPIError
from app.providers.qa_base import QaProvider, QaResult

_JUDGE_PROMPT = (
    "Compare the generated product image to the reference images of the same "
    "jewelry piece. Respond with JSON only: "
    '{"similarity_score": <float 0.0-1.0>, "reasoning": "<short explanation>"}. '
    "1.0 means the generated image faithfully represents the same piece; 0.0 "
    "means it depicts a materially different piece (wrong chain, prong count, "
    "facet geometry, etc)."
)


class GeminiQaProvider(QaProvider):
    def __init__(self, model_version: str) -> None:
        self.model_version = model_version

    def _call_api(self, output_image: bytes, reference_images: list[bytes]) -> dict[str, Any]:
        """Real call via the google-genai SDK. Raises GeminiAPIError for a
        non-2xx response, TimeoutError/ConnectionError for network
        failures — score() classifies both into a ProviderError.
        """
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        parts = [types.Part.from_text(text=_JUDGE_PROMPT)]
        parts.append(types.Part.from_bytes(data=output_image, mime_type="image/jpeg"))
        for image_bytes in reference_images:
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))

        try:
            response = client.models.generate_content(
                model=self.model_version,
                contents=types.Content(role="user", parts=parts),
                config=types.GenerateContentConfig(response_mime_type="application/json"),
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

    def score(self, output_image: bytes, reference_images: list[bytes]) -> QaResult:
        try:
            raw = self._call_api(output_image, reference_images)
        except GeminiAPIError as exc:
            raise ProviderError(
                exc.message, failure_class=self._classify_status(exc.status_code)
            ) from exc
        except TimeoutError as exc:
            raise ProviderError(
                "Gemini QA request timed out.", failure_class=FailureClass.TRANSIENT_NETWORK
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

    def _parse_response(self, raw: dict[str, Any]) -> QaResult:
        candidates = raw.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderError(
                "Malformed Gemini QA response: no candidates.",
                failure_class=FailureClass.INTERNAL,
                details={"raw": raw},
            )

        candidate = candidates[0]
        finish_reason = candidate.get("finish_reason")
        if finish_reason != "STOP":
            raise ProviderError(
                f"Malformed Gemini QA response: unexpected finish_reason {finish_reason!r}.",
                failure_class=FailureClass.INTERNAL,
                details={"raw": raw},
            )

        try:
            parts = candidate["content"]["parts"]
            text = parts[0]["text"]
            payload = json.loads(text)
            score = payload["similarity_score"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "Malformed Gemini QA response: missing similarity_score.",
                failure_class=FailureClass.INTERNAL,
                details={"raw": raw},
            ) from exc

        if (
            not isinstance(score, int | float)
            or isinstance(score, bool)
            or not (0.0 <= score <= 1.0)
        ):
            raise ProviderError(
                f"Malformed Gemini QA response: similarity_score out of range: {score!r}.",
                failure_class=FailureClass.INTERNAL,
                details={"raw": raw},
            )

        model_version = raw.get("model_version", self.model_version)
        return QaResult(score=float(score), model_version=model_version)
