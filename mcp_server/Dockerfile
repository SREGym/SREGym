FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (curl for healthcheck, kubectl for commands, uv for Python deps)
RUN apt-get update && apt-get install -y \
    curl \
    && ARCH=$(dpkg --print-architecture) \
    && curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/${ARCH}/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

# Add uv to PATH
ENV PATH="/root/.local/bin:$PATH"

# Install the locked dependencies without installing the benchmark package.
# MCP runs from the limited runtime source tree below.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Never copy fault definitions, injectors, or oracle answers into this image.
COPY mcp_server/ ./mcp_server/
COPY sregym/__init__.py ./sregym/
COPY sregym/service/__init__.py \
    sregym/service/k8s_proxy.py \
    sregym/service/kubernetes_access_policy.py \
    sregym/service/kubernetes_response.py ./sregym/service/
COPY clients/__init__.py ./clients/
COPY clients/stratus/__init__.py ./clients/stratus/
COPY clients/stratus/configs/langgraph_tool_configs.py ./clients/stratus/configs/
COPY clients/stratus/stratus_utils/__init__.py \
    clients/stratus/stratus_utils/get_logger.py ./clients/stratus/stratus_utils/

# Fail the build if a required runtime import is missing.
RUN .venv/bin/python -c "import mcp_server.sregym_mcp_server"

# Environment defaults (can be overridden at runtime)
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app
ENV MCP_SERVER_PORT=9954
ENV EXPOSE_SERVER=true
ENV SESSION_CACHE_SIZE=10000
ENV SESSION_TTL=600

EXPOSE 9954

CMD ["python", "-m", "mcp_server.sregym_mcp_server"]
