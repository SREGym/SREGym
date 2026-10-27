#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/npm-release-age.sh"
VERSION="${AGENT_VERSION:-latest}"
echo "[$(date -Iseconds)] Installing Claude Code CLI (version: $VERSION)..."
if [ "$VERSION" = "latest" ]; then
    npm install -g @anthropic-ai/claude-code
else
    npm install -g "@anthropic-ai/claude-code@$VERSION"
fi
echo "[$(date -Iseconds)] Claude Code CLI installed: $(claude --version)"
