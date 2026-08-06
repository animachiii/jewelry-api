"""GPU queue: BiRefNet alpha matte extraction.

Phase 0 establishes only the loading location, not the model. Under Celery
prefork, loading the model inside a task body multiplies VRAM by the child
count — the model must load once per worker process, at worker_process_init.
The real model wiring lands in Phase 5.
"""

import structlog
from celery.signals import worker_process_init

logger = structlog.get_logger(__name__)

_matting_model: object | None = None


@worker_process_init.connect  # type: ignore[untyped-decorator]
def load_matting_model(**kwargs: object) -> None:
    global _matting_model
    logger.info("matting_model_would_load_here")
    _matting_model = object()  # sentinel — real BiRefNet singleton lands in Phase 5
