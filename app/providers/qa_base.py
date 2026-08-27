"""QaProvider — the only abstraction boundary a worker task may call
through for QA scoring. Mirrors GenerationProvider (app/providers/base.py).
See docs/ai-integration.md Call Site 2 and phases/phase-9-qa-gate.md.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class QaResult:
    score: float
    model_version: str
    reasoning: str | None = None
    """The judge's own one-line justification for the score. Optional
    because a well-formed response may omit it — a missing reasoning is
    never worth failing a scored call over. Persisted into the QA_SCORED
    event so a flagged output can be explained without re-running the
    judge; see app/services/qa_service.py::_record_qa_scored_event.
    """


class QaProvider(ABC):
    @abstractmethod
    def score(self, output_image: bytes, reference_images: list[bytes], *, prompt: str) -> QaResult:
        """Raises app.core.errors.ProviderError (carrying a failure_class)
        on any failure — same contract as GenerationProvider.generate.
        Callers never inspect raw provider exceptions; the provider
        classifies.

        `prompt` is keyword-only and deliberately has **no default**: what
        the judge is asked differs per call site, and a shared implicit
        default is exactly how background operations ended up being scored
        by the synthetic-angle piece-identity prompt (see
        app/providers/gemini_qa.py's module docstring). Every caller names
        its own judge prompt.
        """
        raise NotImplementedError
