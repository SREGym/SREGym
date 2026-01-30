FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (curl for healthcheck, kubectl for commands, uv for Python deps)
RUN apt-get update && apt-get install -y \
    curl \
    && curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

# Add uv to PATH
ENV PATH="/root/.local/bin:$PATH"

# Copy project files
COPY pyproject.toml uv.lock ./
COPY mcp_server/ ./mcp_server/
COPY sregym/ ./sregym/
COPY clients/ ./clients/
COPY scripts/ ./scripts/

# Install dependencies with uv
RUN uv sync --frozen --no-dev

# Environment defaults (can be overridden at runtime)
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app
ENV MCP_SERVER_PORT=9954
ENV EXPOSE_SERVER=true
ENV SESSION_CACHE_SIZE=10000
ENV SESSION_TTL=600

EXPOSE 9954

CMD ["python", "-m", "mcp_server.sregym_mcp_server"]
