ARG KIND_NODE_IMAGE=kindest/node:v1.32.5@sha256:36187f6c542fa9b78d2d499de4c857249c5a0ac8cc2241bef2ccd92729a7a259
FROM ${KIND_NODE_IMAGE}

# Install udev and related packages
RUN apt-get update && apt-get install -y udev socat && rm -rf /var/lib/apt/lists/*
