# Architecture-neutral equivalent of the vendored load-generator recipe.
FROM python:3.12-slim
ARG TARGETARCH
WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates wget unzip \
    && rm -rf /var/lib/apt/lists/*
RUN wget -q "https://github.com/apache/openwhisk-cli/releases/download/1.2.0/OpenWhisk_CLI-1.2.0-linux-${TARGETARCH}.tgz" -O /tmp/wsk.tgz \
    && tar -xzf /tmp/wsk.tgz -C /usr/local/bin wsk \
    && chmod +x /usr/local/bin/wsk \
    && rm /tmp/wsk.tgz

COPY run-all.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh
ENV REDIS_HOST=owdev-redis.openwhisk.svc.cluster.local REDIS_PORT=6379 REDIS_DB=1
