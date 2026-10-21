ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/slack-main@sha256:7910f6ae9706a6cc4a19f8e2c78c49e0f98c0ae4728aa4045fb33221b9e6d74a
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original

FROM --platform=$BUILDPLATFORM golang:1.22.7-bookworm@sha256:af1bb8730643ca372b0fe9613a17300ade3ad113d36328e2a7e604ccb2b1584b AS minio-build
ARG TARGETARCH
WORKDIR /src
COPY --from=minio-source . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -p 2 -trimpath \
    -ldflags='-s -w -X github.com/minio/minio/cmd.Version=2024-09-13T20:26:02Z -X github.com/minio/minio/cmd.ReleaseTag=RELEASE.2024-09-13T20-26-02Z -X github.com/minio/minio/cmd.CommitID=5bf41aff1702adf2a11fa96a7b68f8651191e24d -X github.com/minio/minio/cmd.ShortCommitID=5bf41aff1702 -X github.com/minio/minio/cmd.CopyrightYear=2024' \
    -o /out/minio .

FROM --platform=$BUILDPLATFORM golang:1.22.7-bookworm@sha256:af1bb8730643ca372b0fe9613a17300ade3ad113d36328e2a7e604ccb2b1584b AS mc-build
ARG TARGETARCH
WORKDIR /src
COPY --from=mc-source . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -p 2 -trimpath \
    -ldflags='-s -w -X github.com/minio/mc/cmd.Version=2024-09-09T07:53:10Z -X github.com/minio/mc/cmd.ReleaseTag=RELEASE.2024-09-09T07-53-10Z -X github.com/minio/mc/cmd.CommitID=04c5116c9bdf8b762acc54b5500a9a276a5ec05a -X github.com/minio/mc/cmd.ShortCommitID=04c5116c9bdf -X github.com/minio/mc/cmd.CopyrightYear=2024' \
    -o /out/mc .

FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS runtime-arm64
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=minio-build /out/minio /usr/bin/minio
COPY --from=mc-build /out/mc /usr/bin/mc
COPY --from=original /usr/bin/docker-entrypoint.sh /usr/bin/docker-entrypoint.sh
COPY --from=original /licenses /licenses
ENV MINIO_ACCESS_KEY_FILE=access_key MINIO_SECRET_KEY_FILE=secret_key \
    MINIO_ROOT_USER_FILE=access_key MINIO_ROOT_PASSWORD_FILE=secret_key \
    MINIO_KMS_SECRET_KEY_FILE=kms_master_key \
    MINIO_UPDATE_MINISIGN_PUBKEY=RWTx5Zr1tiHQLwG9keckT0c45M3AGeHD6IvimQHpyRywVWGbP1aVSGav \
    MINIO_CONFIG_ENV_FILE=config.env MC_CONFIG_DIR=/tmp/.mc
EXPOSE 9000
VOLUME ["/data"]
ENTRYPOINT ["/usr/bin/docker-entrypoint.sh"]
CMD ["minio"]

FROM original AS runtime-amd64
FROM runtime-${TARGETARCH}
