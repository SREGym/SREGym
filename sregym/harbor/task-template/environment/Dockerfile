# Agent workstation. The cluster runs in the privileged sregym sidecar; this
# container only reaches it through SREGym's filtered Kubernetes API proxy.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl dnsutils git iputils-ping jq less netcat-openbsd procps \
    python3 tmux vim-tiny \
    && rm -rf /var/lib/apt/lists/*

ARG KUBECTL_VERSION={{kubectl_version}}
RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl

COPY sregym-ready /usr/local/bin/sregym-ready
RUN chmod +x /usr/local/bin/sregym-ready

# Published by the sidecar on a read-only shared volume once the fault is live.
ENV KUBECONFIG={{agent_shared_dir}}/{{kubeconfig_name}}
WORKDIR /root
