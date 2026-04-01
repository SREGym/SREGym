#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_CTX="$SCRIPT_DIR/build-context"

echo "==> Assembling build context..."
rm -rf "$BUILD_CTX"

SREGYM="$BUILD_CTX/sregym"

# ───────────────────────────────────────────────
# 1. Create directory tree
# ───────────────────────────────────────────────
mkdir -p "$BUILD_CTX"
mkdir -p "$SREGYM/service/apps"

# ───────────────────────────────────────────────
# 2. Copy top-level modules
# ───────────────────────────────────────────────
cp -r "$REPO_ROOT/clients"    "$BUILD_CTX/clients"
cp -r "$REPO_ROOT/logger"     "$BUILD_CTX/logger"
cp -r "$REPO_ROOT/llm_backend" "$BUILD_CTX/llm_backend"

# ───────────────────────────────────────────────
# 3. sregym — package init files
# ───────────────────────────────────────────────
cp "$REPO_ROOT/sregym/__init__.py"                     "$SREGYM/__init__.py"
cp "$REPO_ROOT/sregym/service/__init__.py"             "$SREGYM/service/__init__.py"

# ───────────────────────────────────────────────
# 4. sregym — core modules
# ───────────────────────────────────────────────
cp "$REPO_ROOT/sregym/paths.py"               "$SREGYM/paths.py"
cp "$REPO_ROOT/sregym/service/kubectl.py"     "$SREGYM/service/kubectl.py"
cp "$REPO_ROOT/sregym/service/helm.py"        "$SREGYM/service/helm.py"
cp "$REPO_ROOT/sregym/service/apps/base.py"   "$SREGYM/service/apps/base.py"
cp "$REPO_ROOT/sregym/service/apps/helpers.py" "$SREGYM/service/apps/helpers.py"

echo "==> sregym modules copied ($(find "$SREGYM" -type f | wc -l) files)"

# ───────────────────────────────────────────────
# 5. Build support files
# ───────────────────────────────────────────────
cp -r "$SCRIPT_DIR/install-scripts"          "$BUILD_CTX/install-scripts"
cp "$SCRIPT_DIR/requirements-container.txt"  "$BUILD_CTX/requirements-container.txt"
cp "$SCRIPT_DIR/Dockerfile"                  "$BUILD_CTX/Dockerfile"

# ───────────────────────────────────────────────
# 6. Build image & clean up
# ───────────────────────────────────────────────

# Capture the current image ID so we can remove it after the new build
OLD_IMAGE_ID="$(docker images -q sregym-agent-base:latest 2>/dev/null || true)"

echo "==> Building Docker image..."
docker build --build-arg CACHE_BUST="$(date +%s)" -t sregym-agent-base:latest -f "$BUILD_CTX/Dockerfile" "$BUILD_CTX"

# Remove the previous image (now untagged) to avoid dangling buildup
if [ -n "$OLD_IMAGE_ID" ]; then
    NEW_IMAGE_ID="$(docker images -q sregym-agent-base:latest)"
    if [ "$OLD_IMAGE_ID" != "$NEW_IMAGE_ID" ]; then
        echo "==> Removing previous image $OLD_IMAGE_ID..."
        docker rmi "$OLD_IMAGE_ID" 2>/dev/null || true
    fi
fi

echo "==> Cleaning up build context..."
rm -rf "$BUILD_CTX"

echo "==> Done! Image: sregym-agent-base:latest"
