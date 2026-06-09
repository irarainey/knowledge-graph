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

# Query-safety limits applied to every Cypher statement the agent runs against the graph
# (defence-in-depth on top of neo4j-graphrag's read-only EXPLAIN check). Tunable via the
# environment; sensible PoC defaults otherwise.
ENV_QUERY_TIMEOUT_SECONDS = "QUERY_TIMEOUT_SECONDS"
QUERY_TIMEOUT_SECONDS_DEFAULT = 10.0
ENV_QUERY_ROW_CAP = "QUERY_ROW_CAP"
QUERY_ROW_CAP_DEFAULT = 1000
