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
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl && mv kubectl /usr/local/bin/

# ───────────────────────────────────────────────
# 4. Python dependencies
# ───────────────────────────────────────────────
COPY requirements-container.txt /tmp/requirements-container.txt
RUN python3 -m pip install --break-system-packages -r /tmp/requirements-container.txt

# ───────────────────────────────────────────────
# 5. Application code
# ───────────────────────────────────────────────
ARG CACHE_BUST=0
COPY clients/        /opt/sregym/clients/
COPY logger/         /opt/sregym/logger/
COPY llm_backend/    /opt/sregym/llm_backend/
COPY sregym/         /opt/sregym/sregym/
COPY install-scripts/ /opt/sregym/install-scripts/
RUN chmod +x /opt/sregym/install-scripts/*.sh

# ───────────────────────────────────────────────
# 6. Runtime setup
# ───────────────────────────────────────────────
ENV PYTHONPATH="/opt/sregym"

# SREGym-applications/ is bind-mounted at runtime, not baked in.
RUN mkdir -p /logs
WORKDIR /logs

ENTRYPOINT ["/bin/bash", "-c"]
