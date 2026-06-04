"""Logging configuration for the knowledge-graph backend.

Provides a single application root logger (named ``kg``) that every module logs
under via :func:`get_logger`. :func:`setup_logging` is called once at the entry
point (the API's ``app`` module, the import script) to attach a stdout handler at
the configured level; module loggers created with :func:`get_logger` inherit it.
"""

from __future__ import annotations

import logging
import sys

_LOGGER_NAME = "kg"

# Third-party libraries that log verbosely at INFO/DEBUG; pinned to WARNING so the
# application's own logs stay readable.
_NOISY_LIBRARIES = (
    "opentelemetry",
    "opentelemetry.exporter.otlp.proto.grpc.exporter",
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "neo4j",
    "neo4j.notifications",
    "httpx",
    "httpcore",
    "openai",
)


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Initialise and return the application root logger.

    Idempotent: if the root logger already has handlers (e.g. ``setup_logging`` was
    called twice, or by both the API and a script in one process) it is returned
    unchanged. ``propagate`` is disabled so records are not also emitted by a root
    handler that the server (uvicorn) may install, which would duplicate every line.
    """
    logger = logging.getLogger(_LOGGER_NAME)

    if logger.handlers:
        return logger

    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(log_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False

    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the application root (e.g. ``kg.app``)."""
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
