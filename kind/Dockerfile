ARG KIND_NODE_IMAGE=kindest/node:v1.32.1
FROM ${KIND_NODE_IMAGE}

# Install udev and related packages
RUN apt-get update && apt-get install -y udev socat && rm -rf /var/lib/apt/lists/*
