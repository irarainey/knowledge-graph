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


class _Drop404AccessFilter(logging.Filter):
    """Drop ``uvicorn.access`` records for 404 responses.

    When the service is exposed on a forwarded port it attracts automated discovery
    scanners (e.g. Microsoft Defender for Endpoint probing for Log4Shell/Struts paths)
    that hammer non-existent URLs. Those all return 404 and bury the real request log.
    Uvicorn formats each access record with a positional 5-tuple of
    ``(client_addr, method, path, http_version, status_code)``, so the status is read
    from ``record.args[4]``; anything that doesn't match that shape is left untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            try:
                return int(str(args[4])) != 404
            except (TypeError, ValueError):
                return True
        return True


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

    # Silence 404-spam from port scanners so the access log stays readable.
    logging.getLogger("uvicorn.access").addFilter(_Drop404AccessFilter())

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the application root (e.g. ``kg.app``)."""
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
