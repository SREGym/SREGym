ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/slack-app-builder@sha256:5201d7a4c6e4313c2b9d01209ff6aed413af6f4cc2e11d667631f0ce4920fa07
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original

FROM node:22.23.2-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9 AS runtime-arm64
RUN corepack enable && corepack prepare pnpm@11.24.0 --activate
COPY --from=original /workspace /workspace
COPY --from=original /seed /seed
COPY --from=original /runtime-seed /runtime-seed
WORKDIR /workspace
RUN test -z "$(find /workspace /runtime-seed -type f \( -name '*.node' -o -name '*.so' \) -print -quit)" \
    && pnpm -r exec tsc -b --force

FROM original AS runtime-amd64
FROM runtime-${TARGETARCH}
