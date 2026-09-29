#!/usr/bin/env bash
# Optional development build. Released images are pulled by ContainerRunner.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE="${SREGYM_AGENT_IMAGE:-sregym-agent-base:latest}"

# Match the host client unless the caller pins a version explicitly.
if [[ -z "${KUBECTL_VERSION:-}" ]] && command -v kubectl >/dev/null 2>&1; then
    KUBECTL_VERSION="$(kubectl version --client --output=yaml | awk '$1 == "gitVersion:" { print $2; exit }')"
fi
KUBECTL_VERSION="${KUBECTL_VERSION:-stable}"

echo "==> Building ${IMAGE} with kubectl ${KUBECTL_VERSION}"
# Dockerfile.dockerignore and explicit COPY instructions keep the context small
# without assembling (or deleting) a second source tree in the checkout.
docker build --build-arg KUBECTL_VERSION="$KUBECTL_VERSION" \
    --tag "$IMAGE" --file "$SCRIPT_DIR/Dockerfile" "$REPO_ROOT"
