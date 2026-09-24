#!/usr/bin/env bash
set -euo pipefail
VERSION="${AGENT_VERSION:-latest}"
if [ "$VERSION" != "latest" ]; then
    echo "[$(date -Iseconds)] Cursor CLI installer does not support pinning a version; ignoring AGENT_VERSION=$VERSION" >&2
fi
echo "[$(date -Iseconds)] Installing Cursor CLI..."
# Ignore build-machine ownership so extraction works without CAP_CHOWN.
curl https://cursor.com/install -fsS | TAR_OPTIONS=--no-same-owner bash

# The installer places the binary under ~/.local/bin, which is only added to
# PATH by shell rc files. Later steps run as separate, non-login processes
# chained with '&&', so link it into a directory already on PATH. This must
# be a symlink, not a copy: the installed `agent` script locates its bundled
# node runtime relative to its own resolved (symlink-followed) path.
ln -sf "$HOME/.local/bin/agent" /usr/local/bin/agent

echo "[$(date -Iseconds)] Cursor CLI installed: $(agent --version)"
