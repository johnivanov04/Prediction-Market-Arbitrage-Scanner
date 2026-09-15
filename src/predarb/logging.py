"""Structured logging setup.

Two constraints shape this module:

* Per-message book updates must never be logged at INFO. A single busy market
  produces thousands of deltas a minute; at INFO the log becomes useless and
  the process becomes I/O bound. Update-level detail belongs at DEBUG.
* Every log line that concerns a candidate opportunity carries the identifiers
  needed to audit it later (opportunity id, relation id, book sequence), so
  that the log and the stored evidence can be reconciled.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

__all__ = ["configure_logging", "get_logger"]


def configure_logging(*, level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog and the stdlib root logger.

    ``fmt`` is ``"json"`` for machine-readable output (the default, so captured
    runs stay greppable and parseable) or ``"console"`` for local development.
    """
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if fmt == "console":
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.format_exc_info)
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for ``name``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
