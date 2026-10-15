ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/slack-app@sha256:e050b1d953721145ee580e1baf1e424c55aa31c37a15c909e296c9607c8f7f4a
ARG VARIANT=normal
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original

FROM node:22.23.2-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9 AS runtime-arm64-normal
# The published payload contains JavaScript only, including each release's bug.
COPY --from=original /runtime /runtime
WORKDIR /runtime
ENV NODE_ENV=production
RUN test -z "$(find /runtime -type f \( -name '*.node' -o -name '*.so' \) -print -quit)" \
    && node --check dist/main.js
USER 10001:10001
EXPOSE 8000
CMD ["node", "dist/main.js"]

FROM runtime-arm64-normal AS runtime-arm64-seq-lock
ENV HOLD_SEQ_LOCK=chan-0
FROM original AS runtime-amd64-normal
FROM original AS runtime-amd64-seq-lock
FROM runtime-${TARGETARCH}-${VARIANT}
