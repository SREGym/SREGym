# Preserve the original OpenWhisk Python 3.6 runtime contract and packages.
# Its Python proxy is portable; native dependencies must be rebuilt for ARM64.
ARG TARGETARCH
FROM --platform=linux/amd64 openwhisk/python3action@sha256:7e7bd46232f2e4d5778bbf13a8a8cd986f69eb4221c8ef70d5eb01393508f714 AS runtime-amd64
FROM python:3.6.10-alpine3.11@sha256:cfde075076d89a4b3f79933c9cc281900dcd0e6a3deb01ffd3a4ac371fab60c8 AS runtime-arm64
RUN apk add --no-cache bash bzip2-dev gcc libc-dev libxslt-dev libxml2-dev \
      libffi-dev linux-headers openssl-dev
COPY python-runtime-requirements.txt /tmp/requirements.txt
# Old gevent requires Cython's pre-3.x code generator when built from source.
RUN pip install --no-cache-dir Cython==0.29.36 \
    && pip install --no-cache-dir --no-build-isolation -r /tmp/requirements.txt
COPY --from=runtime-amd64 /actionProxy/ /actionProxy/
COPY --from=runtime-amd64 /pythonAction/ /pythonAction/
RUN mkdir /action
ENV FLASK_PROXY_PORT=8080
CMD ["/bin/bash", "-c", "cd pythonAction && python -u pythonrunner.py"]
FROM runtime-${TARGETARCH}
