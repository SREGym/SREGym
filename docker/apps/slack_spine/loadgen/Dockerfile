ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/slack-loadgen@sha256:5a8bfa7e291a3e9ddc32eca99d291531964f08e3b1e269bd59fc9236a1eebbd2
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original
FROM original AS dependencies
RUN python -m pip freeze > /requirements.txt

FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtime-arm64
RUN apt-get update && apt-get install -y --no-install-recommends curl postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10002 --shell /usr/sbin/nologin loadgen
COPY --from=dependencies /requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=original /app /app
WORKDIR /app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN python -c "import cryptography, yaml, loadgen.runner, loadgen_slack.drivers, maintenance_controller"
EXPOSE 9100
CMD ["python", "loadgen_sidecar.py"]

FROM original AS runtime-amd64
FROM runtime-${TARGETARCH}
