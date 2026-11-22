ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/saleor-loadgen@sha256:cdee1728463bbaa0c2665a809bb39b5c7919a48a4575fd629b3c4f6a857f812e
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original
FROM original AS dependencies
RUN python -m pip freeze > /requirements.txt

FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtime-arm64
RUN apt-get update && apt-get install -y --no-install-recommends curl postgresql-client \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dependencies /requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=original /app /app
WORKDIR /app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
RUN python -c "import yaml, psycopg, loadgen.runner, loadgen_saleor.drivers"
CMD ["python", "-u", "/app/loadgen_sidecar.py"]

FROM original AS runtime-amd64
FROM runtime-${TARGETARCH}
