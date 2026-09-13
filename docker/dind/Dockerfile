FROM ghcr.io/astral-sh/uv:0.8.22 AS uv
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git docker.io iptables iproute2 procps util-linux \
    python3 python3-dev python3-venv build-essential libffi-dev libssl-dev \
    sudo jq wget unzip socat udev tini e2fsprogs \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/

ARG KIND_VERSION=v0.27.0
ARG KUBECTL_VERSION=v1.32.1
ARG HELM_VERSION=v4.0.0
RUN set -eu; arch="$(dpkg --print-architecture)"; \
    curl -fsSL "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${arch}" -o /usr/local/bin/kind; \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" -o /usr/local/bin/kubectl; \
    curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz" -o /tmp/helm.tar.gz; \
    tar -xzf /tmp/helm.tar.gz -C /tmp; \
    mv "/tmp/linux-${arch}/helm" /usr/local/bin/helm; \
    chmod +x /usr/local/bin/kind /usr/local/bin/kubectl; \
    rm -rf /tmp/helm.tar.gz "/tmp/linux-${arch}"

WORKDIR /opt/sregym
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN test -f SREGym-applications/README.md && uv sync --frozen --no-dev --no-build-isolation \
    && chmod +x docker/dind/entrypoint.sh docker/dind/smoke.sh

# The benchmark, daemon and all nested bind-mount sources share this filesystem.
# Every outer container gets its own Docker data volume, kubeconfig and cache.
ENV PATH="/opt/sregym/.venv/bin:${PATH}" \
    DOCKER_HOST=unix:///var/run/docker.sock \
    KIND_EXPERIMENTAL_PROVIDER=docker \
    CALICO_TIMEOUT=600s \
    WAIT_FOR_POD_READY_TIMEOUT=1800
VOLUME /var/lib/docker
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/sregym/docker/dind/entrypoint.sh"]
CMD ["sleep", "infinity"]
