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

# External document content store (Area 4 — scalability). Document *bodies* live outside the
# graph; the graph holds only metadata (the index). The local store reads content files from
# this directory, keyed by each Document node's opaque `storageRef`. An Azure Blob backend is
# provided as a documented stub for production. Tunable via the environment.
ENV_DOCUMENT_STORE_PATH = "DOCUMENT_STORE_PATH"
# Default: the repo's seeded content directory (backend/ -> repo root -> data/documents).
DOCUMENT_STORE_PATH_DEFAULT = "../data/documents"

# Maximum number of characters of document body returned to the answer model per fetch, so a
# large externalised document cannot blow the context window. The returned excerpt is truncated
# to this length (a note is appended when truncation occurs).
ENV_DOCUMENT_EXCERPT_CHAR_CAP = "DOCUMENT_EXCERPT_CHAR_CAP"
DOCUMENT_EXCERPT_CHAR_CAP_DEFAULT = 8000
