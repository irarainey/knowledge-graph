#!/usr/bin/env bash
# =============================================================================
# Start the backend API, the Streamlit UI and the Vue frontend together for
# local development.
#
#   ./scripts/start-dev.sh
#
# All run in the foreground; press Ctrl-C once to stop them all. The backend and
# Streamlit UI each live in their own uv project (backend/ and streamlit-ui/),
# the frontend in frontend/ (pnpm). The Neo4j container and backend/.env must
# already be set up.
# =============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

pids=()

cleanup() {
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "🚀 Starting backend API on http://localhost:8080 ..."
(cd "$ROOT_DIR/backend" && uv run uvicorn api:app --app-dir src --reload --host 0.0.0.0 --port 8080) &
pids+=("$!")

echo "🎈 Starting Streamlit UI on http://localhost:8501 ..."
(cd "$ROOT_DIR/streamlit-ui" && uv run streamlit run app.py) &
pids+=("$!")

echo "🖼️  Starting Vue frontend on http://localhost:5173 ..."
(cd "$ROOT_DIR/frontend" && pnpm dev) &
pids+=("$!")

# Exit (and trigger cleanup) as soon as any process stops.
wait -n
