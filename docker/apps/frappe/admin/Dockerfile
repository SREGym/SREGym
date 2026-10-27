ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/frappe-admin-sidecar@sha256:9975944f1d375dea6afb9c35159f978c4d82691f232e4818ff7faff66121cd2d
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original
FROM original AS dependencies
USER root
RUN python -m pip freeze > /requirements.txt

FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtime-arm64
COPY --from=dependencies /requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=original --chmod=0644 /app/server.py /app/server.py
WORKDIR /app
RUN chmod 0755 /app && python -c "import aiohttp"
USER 1000
EXPOSE 8000
CMD ["python", "/app/server.py"]

FROM original AS runtime-amd64
FROM runtime-${TARGETARCH}
