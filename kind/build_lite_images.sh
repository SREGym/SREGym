#!/usr/bin/env bash
# Build the application images that SREGym-Lite cannot pull natively on the
# current host, then load them into a local KIND cluster.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APPLICATIONS_DIR="${REPO_ROOT}/SREGym-applications"
CLUSTER_NAME="${KIND_CLUSTER_NAME:-kind}"

case "$(uname -m)" in
    arm64|aarch64) PLATFORM="linux/arm64" ;;
    x86_64|amd64) PLATFORM="linux/amd64" ;;
    *)
        echo "Unsupported host architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

for command in docker kind; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "Missing required command: ${command}" >&2
        exit 1
    fi
done

if [[ ! -f "${APPLICATIONS_DIR}/hotelReservation/Dockerfile" ]]; then
    echo "SREGym-applications is not initialized; run git submodule update --init --recursive" >&2
    exit 1
fi

SOCIAL_NETWORK_DIR="${APPLICATIONS_DIR}/socialNetwork"

echo "==> Building Hotel Reservation for ${PLATFORM}"
docker build \
    --platform "${PLATFORM}" \
    --tag ghcr.io/sregym/hotel-reservation:latest \
    --file "${APPLICATIONS_DIR}/hotelReservation/Dockerfile" \
    "${APPLICATIONS_DIR}/hotelReservation"

echo "==> Building Locust exporter for ${PLATFORM}"
docker build \
    --platform "${PLATFORM}" \
    --tag containersol/locust_exporter:v0.5.0 \
    --file "${REPO_ROOT}/docker/locust-exporter/Dockerfile" \
    "${REPO_ROOT}/docker/locust-exporter"

echo "==> Building wrk2 traffic generator for ${PLATFORM}"
docker build \
    --platform "${PLATFORM}" \
    --tag deathstarbench/wrk2-client:latest \
    --file "${REPO_ROOT}/docker/wrk2/Dockerfile" \
    "${REPO_ROOT}/docker/wrk2"

echo "==> Building Social Network dependency base for ${PLATFORM} (first build can take a while)"
# This tag is the next build's FROM image. Keep its manifest stable across
# cached rebuilds instead of generating a new timestamped attestation each time.
docker build \
    --platform "${PLATFORM}" \
    --provenance=false \
    --tag yg397/thrift-microservice-deps:xenial \
    --file "${SOCIAL_NETWORK_DIR}/docker/thrift-microservice-deps/cpp/Dockerfile" \
    "${SOCIAL_NETWORK_DIR}"

echo "==> Building Social Network services for ${PLATFORM}"
docker build \
    --platform "${PLATFORM}" \
    --tag deathstarbench/social-network-microservices:latest \
    --file "${SOCIAL_NETWORK_DIR}/Dockerfile" \
    "${SOCIAL_NETWORK_DIR}"

echo "==> Building Social Network OpenResty frontend for ${PLATFORM}"
docker build \
    --platform "${PLATFORM}" \
    --tag yg397/openresty-thrift:xenial \
    --file "${SOCIAL_NETWORK_DIR}/docker/openresty-thrift/xenial/Dockerfile" \
    "${SOCIAL_NETWORK_DIR}/docker/openresty-thrift"

LITE_IMAGES=(
    ghcr.io/sregym/hotel-reservation:latest
    containersol/locust_exporter:v0.5.0
    deathstarbench/wrk2-client:latest
    deathstarbench/social-network-microservices:latest
    yg397/openresty-thrift:xenial
)

if [[ "${PLATFORM}" == "linux/arm64" ]]; then
    echo "==> Pulling the ARM64 Social Network media frontend"
    docker pull --platform "${PLATFORM}" jacksonarthurclark/media-frontend:latest
    LITE_IMAGES+=(jacksonarthurclark/media-frontend:latest)
fi

if kind get clusters | grep -Fxq "${CLUSTER_NAME}"; then
    echo "==> Loading images into KIND cluster ${CLUSTER_NAME}"
    kind load docker-image --name "${CLUSTER_NAME}" "${LITE_IMAGES[@]}"
else
    echo "==> KIND cluster ${CLUSTER_NAME} does not exist yet; images were built but not loaded"
fi

echo "==> Local Lite compatibility images are ready"
