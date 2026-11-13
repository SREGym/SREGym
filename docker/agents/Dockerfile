FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# archive.ubuntu.com can be served through stale transparent caches that pair
# an old Release file with newer package indexes. Keep the mirror overridable,
# and use Canonical's Azure mirror by default to avoid that failure mode.
ARG UBUNTU_MIRROR=http://azure.archive.ubuntu.com/ubuntu

# ───────────────────────────────────────────────
# 1. Install packages
# ───────────────────────────────────────────────
RUN for source_file in /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources; do \
        if [ -f "$source_file" ]; then \
            sed -i "s|http://archive.ubuntu.com/ubuntu|${UBUNTU_MIRROR}|g" "$source_file"; \
        fi; \
    done \
    && for attempt in 1 2 3 4 5; do \
        rm -rf /var/lib/apt/lists/*; \
        apt-get -o Acquire::Retries=5 update && break; \
        if [ "$attempt" = 5 ]; then exit 1; fi; \
        sleep "$((attempt * 10))"; \
    done \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
    && rm -rf /var/lib/apt/lists/*

RUN curl --retry 5 --retry-all-errors --retry-delay 5 -fsSL https://deb.nodesource.com/setup_22.x | bash -

RUN for attempt in 1 2 3 4 5; do \
        rm -rf /var/lib/apt/lists/*; \
        apt-get -o Acquire::Retries=5 update && break; \
        if [ "$attempt" = 5 ]; then exit 1; fi; \
        sleep "$((attempt * 10))"; \
    done \
    && apt-get install -y --no-install-recommends \
        git wget jq \
        nodejs \
        python3.12 python3.12-venv python3.12-dev \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1 \
    && rm -rf /var/lib/apt/lists/*

# ───────────────────────────────────────────────
# 2. Python + pip
# ───────────────────────────────────────────────
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN python3 -m pip install --upgrade 'pip==26.2.1'

ENV PIP_UPLOADED_PRIOR_TO=P7D

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
RUN python3 -m pip install -r /tmp/requirements-container.txt

# ───────────────────────────────────────────────
# 5. Application code
# ───────────────────────────────────────────────
COPY clients/        /opt/sregym/clients/
COPY logger/         /opt/sregym/logger/
COPY llm_backend/    /opt/sregym/llm_backend/
COPY sregym/__init__.py sregym/paths.py /opt/sregym/sregym/
COPY sregym/service/__init__.py sregym/service/kubectl.py sregym/service/helm.py /opt/sregym/sregym/service/
COPY sregym/service/apps/base.py sregym/service/apps/helpers.py /opt/sregym/sregym/service/apps/
COPY docker/agents/install-scripts/ /opt/sregym/install-scripts/
RUN chmod +x /opt/sregym/install-scripts/*.sh

# ───────────────────────────────────────────────
# 6. Runtime setup
# ───────────────────────────────────────────────
ENV PYTHONPATH="/opt/sregym"

# SREGym-applications/ is bind-mounted at runtime, not baked in.
RUN mkdir -p /logs
WORKDIR /logs

ENTRYPOINT ["/bin/bash", "-c"]
