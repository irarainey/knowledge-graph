#!/bin/bash

set -euo pipefail

# Run from the repo root regardless of where the script is invoked from.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Remove any existing neo4j container (running or stopped) so the name is free.
if docker ps -a --format '{{.Names}}' | grep -qx neo4j; then
  docker rm -f neo4j
fi

docker run -d \
  --name neo4j \
  -p 7474:7474 \
  -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password \
  -v $HOME/neo4j/data:/data \
  neo4j:latest