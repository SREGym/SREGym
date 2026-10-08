FROM python:3.12-slim AS public-runtime
WORKDIR /sources
COPY clients/ clients/
COPY logger/ logger/
COPY llm_backend/ llm_backend/
COPY sregym/__init__.py sregym/paths.py sregym/
COPY sregym/service/__init__.py sregym/service/kubectl.py sregym/service/helm.py sregym/service/
COPY sregym/service/apps/base.py sregym/service/apps/helpers.py sregym/service/apps/
COPY docker/agents/install-scripts/ docker/agents/install-scripts/
COPY docker/agents/runtime_context.py docker/agents/runtime_context.py
RUN python docker/agents/runtime_context.py /public --root /sources

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# ───────────────────────────────────────────────
# 1. Install packages
# ───────────────────────────────────────────────
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg software-properties-common \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && add-apt-repository ppa:deadsnakes/ppa

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git wget jq \
        nodejs \
        python3.12 python3.12-venv python3.12-dev \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1 \
    && rm -rf /var/lib/apt/lists/*

# ───────────────────────────────────────────────
# 2. Python + pip
# ───────────────────────────────────────────────
RUN python3 -m ensurepip --upgrade \
    && python3 -m pip install --upgrade pip \
    && ln -sf /usr/bin/python3 /usr/bin/python

# ───────────────────────────────────────────────
# 3. kubectl
# ───────────────────────────────────────────────
ARG TARGETARCH
ARG KUBECTL_VERSION=stable
RUN KUBECTL_ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && case "${KUBECTL_ARCH}" in amd64|arm64) ;; *) echo "Unsupported kubectl architecture: ${KUBECTL_ARCH}" >&2; exit 1 ;; esac \
    && if [ "${KUBECTL_VERSION}" = stable ]; then KUBECTL_VERSION="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"; fi \
    && curl -fsSLo kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl" \
    && chmod +x kubectl && mv kubectl /usr/local/bin/

# ───────────────────────────────────────────────
# 4. Python dependencies
# ───────────────────────────────────────────────
COPY docker/agents/requirements-container.txt /tmp/requirements-container.txt
RUN python3 -m pip install --break-system-packages -r /tmp/requirements-container.txt

# ───────────────────────────────────────────────
# 5. Application code
# ───────────────────────────────────────────────
COPY --from=public-runtime /public/ /opt/runtime/
RUN chmod +x /opt/runtime/install-scripts/*.sh

# ───────────────────────────────────────────────
# 6. Runtime setup
# ───────────────────────────────────────────────
ENV PYTHONPATH="/opt/runtime"
LABEL io.incident.runtime.root="/opt/runtime"

# Application utilities are mounted at runtime, not baked in.
RUN mkdir -p /logs
WORKDIR /logs

ENTRYPOINT ["/bin/bash", "-c"]
