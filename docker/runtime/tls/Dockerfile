FROM ubuntu:22.04

LABEL org.opencontainers.image.source="https://github.com/SREGym/SREGym"

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
