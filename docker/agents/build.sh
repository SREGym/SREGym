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
mkdir -p "$SREGYM/service/metadata"
mkdir -p "$SREGYM/generators/workload"

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
cp "$REPO_ROOT/sregym/service/metadata/__init__.py"    "$SREGYM/service/metadata/__init__.py"
cp "$REPO_ROOT/sregym/generators/__init__.py"          "$SREGYM/generators/__init__.py"
cp "$REPO_ROOT/sregym/generators/workload/__init__.py" "$SREGYM/generators/workload/__init__.py"

# ───────────────────────────────────────────────
# 4. sregym — core modules
# ───────────────────────────────────────────────
cp "$REPO_ROOT/sregym/paths.py"               "$SREGYM/paths.py"
cp "$REPO_ROOT/sregym/service/kubectl.py"     "$SREGYM/service/kubectl.py"
cp "$REPO_ROOT/sregym/service/helm.py"        "$SREGYM/service/helm.py"
cp "$REPO_ROOT/sregym/service/apps/base.py"   "$SREGYM/service/apps/base.py"
cp "$REPO_ROOT/sregym/service/apps/helpers.py" "$SREGYM/service/apps/helpers.py"

# ───────────────────────────────────────────────
# 5. sregym — workload generators (wrk2 only)
# ───────────────────────────────────────────────
cp "$REPO_ROOT/sregym/generators/workload/base.py"                "$SREGYM/generators/workload/base.py"
cp "$REPO_ROOT/sregym/generators/workload/stream.py"              "$SREGYM/generators/workload/stream.py"
cp "$REPO_ROOT/sregym/generators/workload/wrk2.py"                "$SREGYM/generators/workload/wrk2.py"
cp "$REPO_ROOT/sregym/generators/workload/wrk-job-template.yaml"  "$SREGYM/generators/workload/wrk-job-template.yaml"

# ───────────────────────────────────────────────
# 6. sregym — app metadata JSON files
# ───────────────────────────────────────────────
cp "$REPO_ROOT/sregym/service/metadata/"*.json "$SREGYM/service/metadata/"

# ───────────────────────────────────────────────
# 7. sregym — concrete app classes
# ───────────────────────────────────────────────
cp "$REPO_ROOT/sregym/service/apps/social_network.py"    "$SREGYM/service/apps/social_network.py"
cp "$REPO_ROOT/sregym/service/apps/hotel_reservation.py" "$SREGYM/service/apps/hotel_reservation.py"

# Workload Oracle for AstronomyShop, FlightTicket, TrainTicket, FleetCast are not included

echo "==> sregym modules copied ($(find "$SREGYM" -type f | wc -l) files)"

# ───────────────────────────────────────────────
# 8. Build support files
# ───────────────────────────────────────────────
cp -r "$SCRIPT_DIR/install-scripts"          "$BUILD_CTX/install-scripts"
cp "$SCRIPT_DIR/requirements-container.txt"  "$BUILD_CTX/requirements-container.txt"
cp "$SCRIPT_DIR/Dockerfile"                  "$BUILD_CTX/Dockerfile"

# ───────────────────────────────────────────────
# 9. Build image & clean up
# ───────────────────────────────────────────────
echo "==> Building Docker image..."
docker build --build-arg CACHE_BUST="$(date +%s)" -t sregym-agent-base:latest -f "$BUILD_CTX/Dockerfile" "$BUILD_CTX"

echo "==> Cleaning up build context..."
rm -rf "$BUILD_CTX"

echo "==> Done! Image: sregym-agent-base:latest"
