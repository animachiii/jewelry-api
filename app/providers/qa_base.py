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


class QaProvider(ABC):
    @abstractmethod
    def score(self, output_image: bytes, reference_images: list[bytes]) -> QaResult:
        """Raises app.core.errors.ProviderError (carrying a failure_class)
        on any failure — same contract as GenerationProvider.generate.
        Callers never inspect raw provider exceptions; the provider
        classifies.
        """
        raise NotImplementedError
