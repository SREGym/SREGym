ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
FROM --platform=$BUILDPLATFORM ${PYTHON_IMAGE} AS dependencies
ARG TARGETARCH
COPY requirements.lock /tmp/requirements.lock
RUN case "$TARGETARCH" in \
      amd64) platform=manylinux2014_x86_64 ;; \
      arm64) platform=manylinux2014_aarch64 ;; \
      *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && pip install --no-cache-dir --no-compile --only-binary=:all: \
      --platform "$platform" --target /dependencies -r /tmp/requirements.lock \
    && mv /dependencies/bin /entrypoints

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.source="https://github.com/SREGym/SREGym"
WORKDIR /app
COPY --from=dependencies /dependencies/ /usr/local/lib/python3.12/site-packages/
COPY --from=dependencies /entrypoints/ /usr/local/bin/
COPY vendor/solution/app/ /app/app/
COPY vendor/LICENSE vendor/NOTICE vendor/upstream.json /usr/share/doc/swe-marathon-stripe/
COPY backend.py receiver.py /app/
CMD ["python", "backend.py"]
