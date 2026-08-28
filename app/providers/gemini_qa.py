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

**Second live finding, 2026-08-28** — once the parse bug above was fixed and
real scores started coming back, background operations kept flagging anyway,
because there was only ever *one* judge prompt and both call sites shared it.
See SUBJECT_PRESERVATION_JUDGE_PROMPT below for the evidence. `score()` now
takes the prompt as a required keyword argument so neither call site can
silently inherit the other's question, and the judge's `reasoning` — asked
for since Phase 9, parsed away and discarded ever since — is now returned on
QaResult and recorded on the QA_SCORED event.
"""

import json
from typing import Any

from app.config import settings
from app.core.errors import ProviderError
from app.db.models.enums import FailureClass
from app.providers.gemini import GeminiAPIError
from app.providers.qa_base import QaProvider, QaResult

_MAX_REASONING_CHARS = 500

PIECE_IDENTITY_JUDGE_PROMPT = (
    "Compare the generated product image to the reference images of the same "
    "jewelry piece. Respond with JSON only: "
    '{"similarity_score": <float 0.0-1.0>, "reasoning": "<short explanation>"}. '
    "1.0 means the generated image faithfully represents the same piece; 0.0 "
    "means it depicts a materially different piece (wrong chain, prong count, "
    "facet geometry, etc)."
)
"""Synthetic-angle judge (Call Site 2, score_synthetic_angle). The reference
images are clean catalogue shots from the category matrix, and the candidate
is a novel view of that same piece, so "is this the same piece" is the whole
question and framing is not in play.
"""

SUBJECT_PRESERVATION_JUDGE_PROMPT = (
    "You are checking a jewellery background-editing job. The REFERENCE image "
    "is the original photo a seller supplied — often a casual phone snapshot "
    "with hands, fingers, display pillows, mannequins, price tags, packaging, "
    "text overlays, and a cluttered shop background, sometimes at an awkward "
    "angle with the piece folded, draped, or partly hidden. The CANDIDATE "
    "image is the cleaned e-commerce product photo produced from it.\n"
    "\n"
    "The following differences are INTENDED and must NOT reduce the score:\n"
    "- any change of background, backdrop, surface, shadow, or lighting\n"
    "- removal of hands, fingers, mannequins, pillows, stands, props, "
    "packaging, price tags, watermarks, or text\n"
    "- any change of crop, zoom, framing, aspect ratio, or resolution\n"
    "- any change of the piece's orientation, pose, or angle, including the "
    "piece being straightened, unfolded, opened out, or laid flat so that it "
    "is fully visible when the original showed it bunched up or occluded\n"
    "- cleaner, brighter, or more even studio lighting and colour\n"
    "\n"
    "Judge ONLY whether the CANDIDATE shows the SAME PHYSICAL PIECE as the "
    "REFERENCE: the same motif count and arrangement, the same stone layout "
    "and stone colours, the same metal colour, the same structural design and "
    "distinctive features. A part of the piece that was hidden in the "
    "reference and is now visible is expected — judge the parts you can "
    "compare, and do not penalise the piece for being more complete.\n"
    "\n"
    "Respond with JSON only: "
    '{"similarity_score": <float 0.0-1.0>, "reasoning": "<short explanation>"}.\n'
    "Scoring anchors:\n"
    "- 1.0 — the same piece, differing only in the intended ways listed above\n"
    "- 0.95 — the same piece, with trivial softening of fine detail from "
    "re-rendering\n"
    "- 0.6 — recognisably the same piece, but a real detail has changed "
    "(a stone colour, a motif's shape)\n"
    "- 0.3 — the piece has been visibly altered: motifs or stones added or "
    "removed, metal colour changed, structure changed\n"
    "- 0.0 — a different piece, or the piece is missing, unrecognisable, or "
    "badly mangled in the candidate"
)
"""Background-operation judge (score_background_operation). Added 2026-08-28
after production evidence that reusing PIECE_IDENTITY_JUDGE_PROMPT here
flagged correct outputs: for a background operation the "reference" is the
raw input snapshot, not a catalogue shot, and the operation is *instructed*
by migration 0019 to strip hands/props/tags and produce a clean product
photo. Sub-job 6b3eda1e (2026-08-27) turned a bracelet draped over a velvet
pillow in someone's hand into a flawless open-bangle studio shot and was
scored 0.0 — "materially different piece" — because the piece-identity judge
counts pose and framing as identity. The better the generator obeyed 0019,
the more reliably the judge flagged it.

The anchors are deliberately top-heavy so that the existing
`background_qa_similarity_threshold` (0.92, migration 0010) still reads as
"the judge is confident this is the same piece": intended-only differences
land at 0.95-1.0 and pass, while any genuine alteration of the piece drops
to 0.6 or below and flags.
"""


class GeminiQaProvider(QaProvider):
    def __init__(self, model_version: str) -> None:
        self.model_version = model_version

    def _call_api(
        self, output_image: bytes, reference_images: list[bytes], prompt: str
    ) -> dict[str, Any]:
        """Real call via the google-genai SDK. Raises GeminiAPIError for a
        non-2xx response, TimeoutError/ConnectionError for network
        failures — score() classifies both into a ProviderError.
        """
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        # The images are labelled inline rather than just concatenated:
        # both prompts talk about a "candidate" and a "reference", and
        # nothing in a bare sequence of image parts tells the judge which
        # of them is which. Order is unchanged (candidate first, then
        # references) — only the labels are new.
        parts = [types.Part.from_text(text=prompt)]
        parts.append(types.Part.from_text(text="CANDIDATE image (the generated output):"))
        parts.append(types.Part.from_bytes(data=output_image, mime_type="image/jpeg"))
        parts.append(types.Part.from_text(text="REFERENCE image(s):"))
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

    def score(self, output_image: bytes, reference_images: list[bytes], *, prompt: str) -> QaResult:
        try:
            raw = self._call_api(output_image, reference_images, prompt)
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

        # Both judge prompts ask for `reasoning` alongside the score, and
        # it was parsed away and dropped until 2026-08-28 — which is why
        # every "why was this flagged?" investigation had to start by
        # re-downloading the images and re-running the judge by hand.
        # Optional and best-effort: a missing or oddly-typed reasoning is
        # not worth failing an otherwise valid score over. Bounded because
        # it lands in a job_events detail blob.
        reasoning = payload.get("reasoning") if isinstance(payload, dict) else None
        if not isinstance(reasoning, str) or not reasoning.strip():
            reasoning = None
        else:
            reasoning = reasoning.strip()[:_MAX_REASONING_CHARS]

        model_version = raw.get("model_version", self.model_version)
        return QaResult(score=float(score), model_version=model_version, reasoning=reasoning)
