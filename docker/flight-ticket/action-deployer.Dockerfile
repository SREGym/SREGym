# The vendored recipe hardcodes AMD64 for wsk. Keep the same OS, CLI version,
# action sources and startup script, selecting the binary for the target CPU.
FROM ubuntu:20.04
ARG TARGETARCH
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates wget zip unzip virtualenv docker.io \
    && rm -rf /var/lib/apt/lists/*
RUN wget -q "https://github.com/apache/openwhisk-cli/releases/download/1.2.0/OpenWhisk_CLI-1.2.0-linux-${TARGETARCH}.tgz" -O /tmp/wsk.tgz \
    && tar -xzf /tmp/wsk.tgz -C /usr/local/bin wsk \
    && chmod +x /usr/local/bin/wsk \
    && rm /tmp/wsk.tgz

COPY deploy_ow_actions.sh /app/deploy_ow_actions.sh
ARG PYTHON_RUNTIME_IMAGE=ghcr.io/sregym/flight-ticket-python-runtime:20260911-multiarch@sha256:263cf7a52da1b0a9c8e45825b2574f578c811e791f234499af839a588d255f8a
# Use the same multiarch runtime for packaging dependencies and running actions.
RUN sed -i "s|openwhisk/python3action|${PYTHON_RUNTIME_IMAGE}|g" /app/deploy_ow_actions.sh \
    && chmod +x /app/deploy_ow_actions.sh
COPY actions /app/actions
