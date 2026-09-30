ARG TARGETARCH
FROM --platform=linux/amd64 openwhisk/ow-utils:ef725a6@sha256:1c7a71d545cbea1cfb37e3f0e061207c0a78d50e80c4ebd9cace076351f4b4a5 AS runtime-amd64
FROM node:16.18.0-bullseye@sha256:08382f80c9e5ea5dfcc4760f9937ed6e42e9a07cd61f63035ac31d5aa39f367c AS node
FROM adoptopenjdk/openjdk8:jdk8u262-b10@sha256:cbd4e3aa5f8af08c134b26e7160b7cb64ad5d92be200844ce368c5f44ca58d05 AS runtime-arm64
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
      git jq libffi-dev libssl-dev python python-dev python-pip build-essential \
      wget zip unzip locales docker.io \
    && rm -rf /var/lib/apt/lists/*
COPY utility-requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir pip==20.3.4 setuptools==44.1.1 wheel==0.37.1 Cython==0.29.36 \
    && python -m pip install --no-cache-dir --no-build-isolation -r /tmp/requirements.txt
RUN curl -fsSL "https://dl.k8s.io/release/v1.32.1/bin/linux/${TARGETARCH}/kubectl" -o /usr/local/bin/kubectl \
    && curl -fsSL "https://dl.k8s.io/release/v1.32.1/bin/linux/${TARGETARCH}/kubectl.sha256" -o /tmp/kubectl.sha256 \
    && echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod +x /usr/local/bin/kubectl
RUN curl -fsSL "https://github.com/apache/openwhisk-cli/releases/download/1.2.0/OpenWhisk_CLI-1.2.0-linux-${TARGETARCH}.tgz" -o /tmp/wsk.tgz \
    && tar -xzf /tmp/wsk.tgz -C /usr/local/bin wsk && rm /tmp/wsk.tgz \
    && curl -fsSL "https://github.com/apache/openwhisk-wskdeploy/releases/download/1.2.0/openwhisk_wskdeploy-1.2.0-linux-${TARGETARCH}.tgz" -o /tmp/wskdeploy.tgz \
    && tar -xzf /tmp/wskdeploy.tgz -C /usr/local/bin wskdeploy && rm /tmp/wskdeploy.tgz
COPY --from=runtime-amd64 /bin/wskadmin /bin/wskutil.py /bin/wskprop.py /bin/
COPY --from=runtime-amd64 /cert-gen/ /cert-gen/
COPY --from=runtime-amd64 /usr/local/bin/genssl.sh /usr/local/bin/
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/ /usr/local/lib/node_modules/
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
WORKDIR /
CMD ["/bin/bash"]
FROM runtime-${TARGETARCH}
