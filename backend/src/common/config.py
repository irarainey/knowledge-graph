"""Application-wide configuration constants.

Centralises the names of environment variables (and their defaults) read at the
entry points, so they are defined in one place rather than as scattered string
literals.
"""

from __future__ import annotations

# Logging verbosity, e.g. "DEBUG", "INFO", "WARNING". Read at startup.
ENV_LOG_LEVEL = "LOG_LEVEL"
LOG_LEVEL_DEFAULT = "INFO"

# Azure Application Insights connection string. When set, OpenTelemetry traces,
# metrics and logs are exported to Application Insights; unset disables telemetry.
ENV_APPLICATIONINSIGHTS_CONNECTION_STRING = "APPLICATIONINSIGHTS_CONNECTION_STRING"
