# Rebuild the revision reported by radondb/xenon:1.1.5-helm, including
# its Kubernetes-specific changes. Dependencies are vendored upstream.
FROM --platform=linux/amd64 radondb/xenon@sha256:a1b803a7a5e26f7287c6aa7945fce429cb55acc42f1b392eeb0fa7520f560967 AS original

FROM --platform=$BUILDPLATFORM golang:1.13.15 AS build
WORKDIR /src
RUN curl -fsSL https://github.com/radondb/xenon/archive/0abe2495f5db2d56d3d05e8be248c88e10393011.tar.gz \
      | tar -xz --strip-components=1
ARG TARGETOS
ARG TARGETARCH
ENV GOPATH=/src GO111MODULE=off CGO_ENABLED=0
RUN GOOS=${TARGETOS} GOARCH=${TARGETARCH} go build \
      -ldflags '-X build.git=0abe249 -X build.tag=v1.1.4-30-g0abe249' \
      -o /out/xenon src/xenon/xenon.go \
    && GOOS=${TARGETOS} GOARCH=${TARGETARCH} go build \
      -ldflags '-X build.git=0abe249 -X build.tag=v1.1.4-30-g0abe249' \
      -o /out/xenoncli src/cli/cli.go

FROM alpine:3.13
RUN apk add --no-cache curl bash \
    && addgroup -S -g 777 mysql \
    && adduser -S -H -u 777 -G mysql mysql \
    && mkdir -p /etc/xenon /var/lib/xenon \
    && chown mysql:mysql /etc/xenon /var/lib/xenon
COPY --from=build /out/ /usr/local/bin/
COPY --from=build /src/LICENSE /usr/share/licenses/xenon/LICENSE
COPY --from=original /usr/local/bin/docker-entrypoint /usr/local/bin/docker-entrypoint
COPY --from=original /config.path /config.path
USER 777
EXPOSE 8801
VOLUME ["/var/lib/xenon"]
ENTRYPOINT ["docker-entrypoint"]
CMD ["xenon", "-c", "/etc/xenon/xenon.json"]
