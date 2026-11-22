ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/slack-py@sha256:c3a51b1ab5082f337ae5e8c1f2d60467273b1e73440580fc7e1132564b943c12
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original
FROM ghcr.io/astral-sh/uv:0.12.8@sha256:d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a AS uv

FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtime-arm64
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY --from=original /app/pyproject.toml /app/uv.lock ./
RUN uv sync --frozen --no-cache
COPY --from=original /app/pykit ./pykit
COPY --from=original /app/*.py ./
RUN uv run --no-sync python -c "import aiohttp, aiokafka, redis, prometheus_client, pykit"
ENTRYPOINT ["uv", "run", "--no-sync", "python"]
CMD ["scheduler.py"]

FROM original AS runtime-amd64
FROM runtime-${TARGETARCH}
