#!/bin/bash
# =============================================================================
# Dev Container Initialization Script
# =============================================================================

set -e

# Install GitHub Copilot CLI if not present
if ! command -v github-copilot-cli &> /dev/null && ! command -v copilot &> /dev/null; then
    echo "🤖 Installing GitHub Copilot CLI..."
    curl -fsSL https://gh.io/copilot-install | bash
fi

# Install Azure CLI if not present
if ! command -v az &> /dev/null; then
    echo "☁️ Installing Azure CLI..."
    export DEBIAN_FRONTEND=noninteractive
    sudo apt-get update -qq
    sudo apt-get install -y -qq ca-certificates curl apt-transport-https lsb-release gnupg
    sudo mkdir -p /etc/apt/keyrings
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | sudo gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg
    sudo chmod go+r /etc/apt/keyrings/microsoft.gpg
    # Use bookworm (Debian 12) repo — azure-cli doesn't publish a trixie (Debian 13) release yet
    AZ_DIST="bookworm"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ ${AZ_DIST} main" | sudo tee /etc/apt/sources.list.d/azure-cli.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq azure-cli
fi

# Install pnpm if not present
if ! command -v pnpm &> /dev/null; then
    echo "📦 Installing pnpm..."
    corepack enable
    corepack prepare pnpm@latest --activate
fi

# Install Python server dependencies via uv
if [ -f "backend/pyproject.toml" ]; then
    echo "🐍 Installing Python dependencies via uv..."
    (cd backend && uv sync)
fi

# Install frontend dependencies via pnpm
if [ -f "frontend/package.json" ]; then
    echo "🎨 Installing frontend dependencies via pnpm..."
    (cd frontend && pnpm install)
fi

echo "✅ Dev container ready!"