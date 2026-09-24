#!/usr/bin/env bash
set -euo pipefail
VERSION="${AGENT_VERSION:-latest}"
echo "[$(date -Iseconds)] Installing GitHub Copilot CLI (version: $VERSION)..."
if [ "$VERSION" = "latest" ]; then
    npm install -g @github/copilot
else
    npm install -g "@github/copilot@$VERSION"
fi
echo "[$(date -Iseconds)] Copilot CLI installed: $(copilot version)"
