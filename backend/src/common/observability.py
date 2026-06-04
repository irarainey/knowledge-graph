"""Observability configuration — Azure Application Insights integration.

Wires the Microsoft Agent Framework's OpenTelemetry instrumentation up to Azure
Application Insights so traces, metrics and logs from the ``/ask`` pipeline are
exported. Telemetry is opt-in: if ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is
not set the call is a no-op, so local development needs no Azure resources.
"""

from __future__ import annotations

import os

from common.config import ENV_APPLICATIONINSIGHTS_CONNECTION_STRING
from common.logging_config import get_logger

logger = get_logger(__name__)


def setup() -> None:
    """Configure Agent Framework observability with Azure Application Insights.

    Reads ``APPLICATIONINSIGHTS_CONNECTION_STRING`` from the environment. If the
    variable is not set, telemetry is silently skipped (a warning is logged). Any
    configuration failure is logged but never propagated, so a misconfigured
    telemetry backend can't take the application down.
    """
    connection_string = os.getenv(ENV_APPLICATIONINSIGHTS_CONNECTION_STRING)

    if not connection_string:
        logger.warning("%s not set — telemetry disabled", ENV_APPLICATIONINSIGHTS_CONNECTION_STRING)
        return

    try:
        from agent_framework.observability import enable_instrumentation
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(connection_string=connection_string)
        enable_instrumentation()

        logger.info("Observability configured with Azure Application Insights")
    except Exception:
        logger.exception("Failed to configure observability")
