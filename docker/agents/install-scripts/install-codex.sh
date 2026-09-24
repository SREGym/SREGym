#!/usr/bin/env bash
set -euo pipefail
VERSION="${AGENT_VERSION:-latest}"
echo "[$(date -Iseconds)] Installing Codex CLI (version: $VERSION)..."
if [ "$VERSION" = "latest" ]; then
    npm install -g --include=optional @openai/codex
else
    npm install -g --include=optional "@openai/codex@$VERSION"
fi

platform_suffix=""
platform_package=""
case "$(uname -s)/$(uname -m)" in
    Linux/aarch64|Linux/arm64)
        platform_suffix="linux-arm64"
        platform_package="@openai/codex-linux-arm64"
        ;;
    Linux/x86_64|Linux/amd64)
        platform_suffix="linux-x64"
        platform_package="@openai/codex-linux-x64"
        ;;
esac

global_node_modules="$(npm root -g)"
if [ -n "$platform_package" ] && ! NODE_PATH="$global_node_modules" node -e "require.resolve('$platform_package')" >/dev/null 2>&1; then
    codex_root="$global_node_modules/@openai/codex"
    installed_version="$(node -p "require('$codex_root/package.json').version")"
    echo "[$(date -Iseconds)] Installing missing Codex platform package: $platform_package"
    npm install -g "${platform_package}@npm:@openai/codex@${installed_version}-${platform_suffix}"
fi

codex_version="$(codex --version)"
codex exec --help >/dev/null
echo "[$(date -Iseconds)] Codex CLI installed: $codex_version"
