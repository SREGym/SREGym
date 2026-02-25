#!/usr/bin/env bash
set -euo pipefail
VERSION="${AGENT_VERSION:-latest}"
echo "[$(date -Iseconds)] Installing OpenCode (version: $VERSION)..."
if [ "$VERSION" = "latest" ]; then
    npm install -g opencode-ai
else
    npm install -g "opencode-ai@$VERSION"
fi
echo "[$(date -Iseconds)] OpenCode installed: $(opencode --version)"
