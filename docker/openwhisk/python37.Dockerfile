ARG TARGETARCH
FROM --platform=linux/amd64 openwhisk/action-python-v3.7:1.17.0@sha256:4d242a19e72f250ce2d421b6fdd19cd26004c6436fb5c430e209ef093fc163fc AS runtime-amd64
FROM --platform=$BUILDPLATFORM golang:1.19.13-bullseye@sha256:2fdfcb03b1445f06f1cf8a342516bfd34026b527fef8427f40ea7b140168fda2 AS proxy
ARG TARGETARCH
ADD --checksum=sha256:19472b3e851685ef2369823f36f0128db0b6cd1989b40e1676b73ffb2b2b3fa0 https://api.github.com/repos/apache/openwhisk-runtime-go/tarball/1.16@1.18.0 /tmp/proxy.tar.gz
WORKDIR /src
RUN tar -xzf /tmp/proxy.tar.gz --strip-components=1 \
    && cd main && CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -trimpath -o /bin/proxy

FROM python:3.7.11-buster@sha256:f970c215c71ae3bffae6074baeaa3c8dd7c0a385eef0a80020aaca61cd2e61a9 AS runtime-arm64
COPY python37-requirements.txt /requirements.txt
RUN pip install --no-cache-dir Cython==0.29.36 \
    && pip install --no-cache-dir --no-build-isolation -r /requirements.txt \
    && mkdir /action
COPY --from=proxy /bin/proxy /bin/proxy
COPY --from=runtime-amd64 /bin/compile /bin/compile
COPY --from=runtime-amd64 /lib/launcher.py /lib/launcher.py
ENV OW_LOG_INIT_ERROR=1 OW_WAIT_FOR_ACK=1 \
    OW_EXECUTION_ENV=openwhisk/action-python-v3.7 OW_COMPILER=/bin/compile
WORKDIR /
ENTRYPOINT ["/bin/proxy"]
FROM runtime-${TARGETARCH}
