"""GenerationProvider — the only abstraction boundary a worker task may call
through. See docs/ai-integration.md and docs/conventions.md ("app/providers/
is the only place that imports a model SDK"). Task bodies never import
google.genai directly.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class GenerationResult:
    image_bytes: bytes
    mime_type: str
    model_version: str


class GenerationProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str, reference_images: list[bytes], seed: int) -> GenerationResult:
        """Raises app.core.errors.ProviderError (carrying a failure_class)
        on any failure — see docs/ai-integration.md's failure table. Callers
        never inspect raw provider exceptions; the provider classifies.
        """
        raise NotImplementedError
