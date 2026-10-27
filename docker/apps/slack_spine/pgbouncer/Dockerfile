ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/slack-pgbouncer@sha256:70dc0e4aefb23ba0646a3410358732ba8358f3b8aa95b8595f2decbba56ceff0
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original

FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS runtime-arm64
RUN apt-get update && apt-get install -y --no-install-recommends pgbouncer ca-certificates \
    && rm -rf /var/lib/apt/lists/*
USER postgres
ENTRYPOINT ["pgbouncer"]
CMD ["/etc/pgbouncer/pgbouncer.ini"]

FROM original AS runtime-amd64
FROM runtime-${TARGETARCH}
