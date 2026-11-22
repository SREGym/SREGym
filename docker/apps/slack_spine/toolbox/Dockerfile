ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/slack-main@sha256:e3dec9737ddfb496b7c380ff529248bf4216514f0d0127f91e41973c05d940f9
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original

FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS runtime-arm64
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl ca-certificates tar vim-tiny python3 python3-yaml postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /bin/bash agent \
    && chmod 0711 /home/agent \
    && printf '%s\n' 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99-sre-world-agent-bootstrap \
    && install -d -o root -g root -m 0755 /var/lib/apt/lists/partial /var/cache/apt/archives/partial
RUN curl -fL --retry 3 -o /tmp/kubectl "https://dl.k8s.io/release/v1.32.2/bin/linux/${TARGETARCH}/kubectl" \
    && curl -fL --retry 3 -o /tmp/kubectl.sha256 "https://dl.k8s.io/release/v1.32.2/bin/linux/${TARGETARCH}/kubectl.sha256" \
    && echo "$(cat /tmp/kubectl.sha256)  /tmp/kubectl" | sha256sum --check --strict \
    && install -m 0755 /tmp/kubectl /usr/local/bin/kubectl && rm /tmp/kubectl /tmp/kubectl.sha256
COPY --from=original /usr/local/bin/*.sh /usr/local/bin/submit_incident_report /usr/local/bin/declare_repair_complete /usr/local/bin/inspect-stream /usr/local/bin/
COPY --from=original /usr/local/lib/sre-world/agent_freezer.py /usr/local/lib/sre-world/agent_freezer.py
RUN python3 -c "import yaml" && command -v psql && bash -n /usr/local/bin/reconfigure-infra.sh
CMD ["/bin/bash"]

FROM original AS runtime-amd64
FROM runtime-${TARGETARCH}
