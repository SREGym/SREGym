# Keep v0.12.1: the chart uses its DATA_SOURCE_NAME environment variable.
# Upstream publishes both binaries, but its old image index omits ARM64.
FROM --platform=$BUILDPLATFORM alpine:3.22 AS download
RUN apk add --no-cache ca-certificates curl
ARG TARGETARCH
RUN case "$TARGETARCH" in \
      amd64) checksum=133b0c281e5c6f8a34076b69ade64ab6cac7298507d35b96808234c4aa26b351 ;; \
      arm64) checksum=b152cbf36ca2ea7e5632cc51f0a1f69bc10e90e1a10dac102a57e8c98e99f2a7 ;; \
      *) exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/prometheus/mysqld_exporter/releases/download/v0.12.1/mysqld_exporter-0.12.1.linux-${TARGETARCH}.tar.gz" -o /tmp/exporter.tgz \
    && echo "$checksum  /tmp/exporter.tgz" | sha256sum -c - \
    && mkdir /out \
    && tar -xzf /tmp/exporter.tgz -C /out --strip-components=1
FROM alpine:3.22
COPY --from=download /out/mysqld_exporter /bin/mysqld_exporter
COPY --from=download /out/LICENSE /out/NOTICE /usr/share/licenses/mysqld-exporter/
COPY --from=download /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
USER nobody
EXPOSE 9104
ENTRYPOINT ["/bin/mysqld_exporter"]
