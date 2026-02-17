#!/usr/bin/env bash
set -euo pipefail
VERSION="${AGENT_VERSION:-latest}"
echo "[$(date -Iseconds)] Installing Gemini CLI (version: $VERSION)..."
if [ "$VERSION" = "latest" ]; then
    npm install -g @google/gemini-cli
else
    npm install -g "@google/gemini-cli@$VERSION"
fi
echo "[$(date -Iseconds)] Gemini CLI installed: $(gemini --version)"
