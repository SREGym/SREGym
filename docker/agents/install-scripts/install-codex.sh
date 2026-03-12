#!/usr/bin/env bash
set -euo pipefail
VERSION="${AGENT_VERSION:-latest}"
echo "[$(date -Iseconds)] Installing Codex CLI (version: $VERSION)..."
if [ "$VERSION" = "latest" ]; then
    npm install -g @openai/codex
else
    npm install -g "@openai/codex@$VERSION"
fi
echo "[$(date -Iseconds)] Codex CLI installed: $(codex --version)"
