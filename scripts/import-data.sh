#!/bin/bash
# Import the knowledge graph JSON into Neo4j.
#
# Usage:
#   scripts/import-data.sh            # update / upsert existing graph
#   scripts/import-data.sh --clear    # delete everything, then import
#
# Any extra arguments (e.g. --clear, --file <path>) are forwarded to the
# underlying Python import script. Neo4j settings are read from backend/.env.

set -euo pipefail

# Resolve the repo root from this script's location so it works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/../backend" && pwd)"

cd "$BACKEND_DIR"
exec uv run poe import-graph "$@"
