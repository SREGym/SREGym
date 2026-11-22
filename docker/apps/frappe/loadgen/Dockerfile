ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/frappe-loadgen@sha256:0372494536eb9a0b40745cdc92a1915ec5b144a67f8bc509845eba5e411b310c
# Preserve the published AMD64 runtime; reuse only its Python source on ARM.
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original
FROM original AS dependencies
RUN python -m pip freeze > /requirements.txt

FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtime-arm64
RUN apt-get update && apt-get install -y --no-install-recommends curl mariadb-client \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dependencies /requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=original /app /app
WORKDIR /app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
RUN python -c "import yaml, aiohttp, httpx, loadgen.runner, loadgen_frappe.drivers"
CMD ["python", "-u", "/app/loadgen_sidecar.py"]

FROM original AS runtime-amd64
FROM runtime-${TARGETARCH}
