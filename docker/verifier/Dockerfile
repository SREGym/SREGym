ARG PYTHON_VERSION=3.12
FROM ghcr.io/astral-sh/uv:0.8.22 AS uv
FROM python:${PYTHON_VERSION}-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git build-essential libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ARG KUBECTL_VERSION=v1.32.1
ARG HELM_VERSION=v4.0.0
RUN set -eu; arch="$(dpkg --print-architecture)"; \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" -o /usr/local/bin/kubectl; \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl.sha256" -o /tmp/kubectl.sha256; \
    echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum --check; \
    chmod +x /usr/local/bin/kubectl; \
    curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz" -o /tmp/helm.tar.gz; \
    tar -xzf /tmp/helm.tar.gz -C /tmp; \
    mv "/tmp/linux-${arch}/helm" /usr/local/bin/helm; \
    rm -rf /tmp/*
WORKDIR /opt/sregym
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python \
    && uv cache clean
COPY sregym/ ./sregym/
COPY logger/ ./logger/
COPY clients/ ./clients/
COPY llm_backend/ ./llm_backend/
COPY SREGym-applications/ ./SREGym-applications/
ENV PATH="/opt/sregym/.venv/bin:${PATH}" PYTHONPATH=/opt/sregym \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp/verifier-home
USER 10001:10001
ENTRYPOINT ["python", "-m", "sregym.service.verifier_worker"]
