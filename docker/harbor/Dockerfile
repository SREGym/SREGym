# Single-container, unprivileged Harbor task image: a four-node k3s cluster
# (docker/userns-k3s), the SREGym backend that deploys and grades the problem,
# and the agent's tools. Needs no --privileged, capabilities or devices; see
# docs/harbor.md for the hosts it runs on.
FROM rancher/k3s:v1.32.5-k3s1@sha256:b75eac801bbc91b4584769c840f346683c7740d72f83e630088370f8e1eb2c8a AS k3s
FROM ghcr.io/astral-sh/uv:0.8.22 AS uv
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git iproute2 iptables procps util-linux socat \
    python3 python3-dev python3-venv build-essential libffi-dev libssl-dev \
    jq tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/

ARG KUBECTL_VERSION=v1.32.1
ARG HELM_VERSION=v4.0.0
RUN set -eu; arch="$(dpkg --print-architecture)"; \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" -o /usr/local/bin/kubectl; \
    curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz" | tar -xz -C /tmp; \
    mv "/tmp/linux-${arch}/helm" /usr/local/bin/helm; \
    chmod +x /usr/local/bin/kubectl; rm -rf "/tmp/linux-${arch}"

# k3s, containerd and runc stay out of /bin: the cluster scripts append this
# directory to their PATH (K3S_BIN), so Ubuntu's tools win over busybox's.
COPY --from=k3s /bin /opt/k3s/bin
RUN mv /opt/k3s/bin/runc /opt/k3s/bin/runc.real
COPY docker/userns-k3s/runc-wrapper /opt/k3s/bin/runc
COPY docker/userns-k3s/entrypoint.sh docker/userns-k3s/fabric.sh docker/userns-k3s/node.sh \
    docker/userns-k3s/egress-proxy.py /usr/local/lib/userns-k3s/
COPY docker/userns-k3s/registries.yaml /etc/userns-k3s/registries.yaml

WORKDIR /opt/sregym
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN test -f SREGym-applications/README.md && uv sync --frozen --no-dev --no-build-isolation

COPY docker/harbor/start.sh /usr/local/lib/sregym-harbor/start.sh
COPY docker/harbor/sregym-ready /usr/local/bin/sregym-ready
# The agent runs as this user (task.toml [agent] user). Problem code, grading,
# cluster state and admin credentials stay root-only; the agent reaches the
# cluster only through SREGym's filtered API proxy, via $KUBECONFIG.
RUN chmod 755 /usr/local/lib/sregym-harbor/start.sh /usr/local/bin/sregym-ready \
    && chmod 700 /opt/sregym /root \
    && useradd -m -s /bin/bash agent

ENV PATH="/opt/sregym/.venv/bin:${PATH}" \
    K3S_BIN=/opt/k3s/bin \
    WAIT_FOR_POD_READY_TIMEOUT=1800 \
    KUBECONFIG=/run/sregym/kubeconfig
WORKDIR /home/agent
CMD ["sleep", "infinity"]
