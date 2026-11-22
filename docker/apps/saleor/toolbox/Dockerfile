ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/saleor-main@sha256:ece6147d06e9f60750b33827302a063cebfa4112e1a37f002dd778b441c09ba5
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original

FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS runtime-arm64
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl jq postgresql-client procps python3 python3-yaml tar util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /bin/bash agent \
    && chmod 0711 /home/agent \
    && install -d -o agent -g agent -m 0755 /logs /logs/agent \
    && install -d -o root -g agent -m 0770 /logs/artifacts \
    && install -d -o root -g root -m 0700 /logs/verifier \
    && printf '%s\n' 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99-sre-world-agent-bootstrap \
    && install -d -o root -g root -m 0755 /var/lib/apt/lists/partial /var/cache/apt/archives/partial
COPY --from=original /usr/local/bin/ /usr/local/bin/
COPY --from=original /usr/local/lib/sre-world/agent_freezer.py /usr/local/lib/sre-world/agent_freezer.py
RUN python3 -c "import yaml" && command -v psql && bash -n /usr/local/bin/declare_repair_complete
CMD ["sleep", "infinity"]

FROM original AS runtime-amd64
FROM runtime-${TARGETARCH}
