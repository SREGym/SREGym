ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/frappe-main@sha256:d40b49eb1c050f14d4fa91ad5bb894ee1db61724466caea903ee0a952574210e
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original

FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS runtime-arm64
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl ca-certificates tar python3 python3-yaml mariadb-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /bin/bash agent \
    && chmod 0711 /home/agent \
    && printf '%s\n' 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99-sre-world-agent-bootstrap \
    && install -d -o root -g root -m 0755 /var/lib/apt/lists/partial /var/cache/apt/archives/partial
COPY --from=original /usr/local/bin/ /usr/local/bin/
COPY --from=original /usr/local/lib/sre-world/agent_freezer.py /usr/local/lib/sre-world/agent_freezer.py
RUN python3 -c "import yaml" && command -v mysql && bash -n /usr/local/bin/reconfigure-infra.sh
CMD ["/bin/bash"]

FROM original AS runtime-amd64
FROM runtime-${TARGETARCH}
