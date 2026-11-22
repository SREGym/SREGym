ARG DEBIAN_IMAGE=debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
FROM ${DEBIAN_IMAGE} AS runtime-base

FROM --platform=$BUILDPLATFORM ${DEBIAN_IMAGE} AS builder
ARG TARGETARCH
COPY --from=runtime-base /var/lib/dpkg/status /tmp/runtime-status
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Download and unpack target packages without executing target binaries.
RUN apt-get -o APT::Architecture="$TARGETARCH" update \
    && mkdir -p /packages/partial /runtime \
    && apt-get -o APT::Architecture="$TARGETARCH" \
      -o Dir::State::status=/tmp/runtime-status \
      -o Dir::Cache::archives=/packages \
      --download-only --no-install-recommends -y install \
      media-types mailcap unrtf wv poppler-utils tidy tzdata \
    && for package in /packages/*.deb; do dpkg-deb --extract "$package" /runtime; done \
    && for directory in bin sbin lib lib64; do \
      if [ -d "/runtime/$directory" ] && [ ! -L "/runtime/$directory" ]; then \
        mkdir -p "/runtime/usr/$directory"; \
        cp -a "/runtime/$directory/." "/runtime/usr/$directory/"; \
        rm -r "/runtime/$directory"; \
      fi; \
    done

RUN case "$TARGETARCH" in \
      amd64) checksum=f7e7d6819af1a4b61f156eb2909e2f17c2e9cff26fb62ff3a78beb2aa7ca5e29 ;; \
      arm64) checksum=4c7424a2aa426dfd8cdacd06945cf9ca6aaa486328cc713fb3c14a1131345e57 ;; \
      *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --retry 3 \
      "https://releases.mattermost.com/11.7.0/mattermost-team-11.7.0-linux-${TARGETARCH}.tar.gz" \
      --output /tmp/mattermost.tar.gz \
    && echo "$checksum  /tmp/mattermost.tar.gz" | sha256sum --check \
    && tar -xzf /tmp/mattermost.tar.gz -C / \
    && mkdir -p /mattermost/data /mattermost/logs /mattermost/plugins \
      /mattermost/client/plugins /mattermost/.postgresql \
    && chmod 700 /mattermost/.postgresql \
    && groupadd --gid 2000 mattermost \
    && useradd --uid 2000 --gid 2000 --home-dir /mattermost mattermost

FROM runtime-base
LABEL org.opencontainers.image.source="https://github.com/SREGym/SREGym" \
      org.opencontainers.image.version="11.7.0" \
      org.opencontainers.image.description="Mattermost Team Edition from official AMD64 and ARM64 release packages"
COPY --from=builder /runtime/ /
COPY --from=builder /etc/ssl/certs/ /etc/ssl/certs/
COPY --from=builder /usr/share/ca-certificates/ /usr/share/ca-certificates/
COPY --from=builder /etc/passwd /etc/group /etc/
COPY --from=builder --chown=2000:2000 /mattermost/ /mattermost/
ENV PATH="/mattermost/bin:${PATH}" \
    MM_SERVICESETTINGS_ENABLELOCALMODE=true \
    MM_INSTALL_TYPE=docker
USER mattermost
WORKDIR /mattermost
HEALTHCHECK --interval=30s --timeout=10s CMD ["/mattermost/bin/mmctl", "system", "status", "--local"]
EXPOSE 8065 8067 8074 8075
VOLUME ["/mattermost/data", "/mattermost/logs", "/mattermost/config", "/mattermost/plugins", "/mattermost/client/plugins"]
CMD ["/mattermost/bin/mattermost"]
