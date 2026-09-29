FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends wget unzip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=population-source . /app/
# Preserve the working upstream dependency set; redis-py 8 defaults to RESP3,
# which the bundled Redis 4 server does not implement.
COPY populate-redis-requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
