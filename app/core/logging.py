"""structlog setup — JSON to stdout, request_id/job_id/etc. correlation via contextvars.

See docs/conventions.md — no `print`, no bare `logging.getLogger`. Context is bound once
at the request/task entry point via `bind_request_context`, not threaded through every
function signature.
"""

import logging
from typing import Any

import structlog

from app.config import settings


def configure_logging() -> None:
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(**initial_values: Any) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(**initial_values)
    return logger


def bind_request_context(**kwargs: Any) -> None:
    """Bind request_id / job_id / sub_job_id / angle / client_id once per request or task."""
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()
