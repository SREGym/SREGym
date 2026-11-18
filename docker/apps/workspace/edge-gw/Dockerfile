# edge-gw (the gateway), workspace-clients (the tenant's user population) and
# edge-load (a standalone load tool for calibration).
FROM --platform=$BUILDPLATFORM golang:1.25-bookworm@sha256:3b4a11519ad929d1e1d261a12cff056f0c85b735253d7d861346b9c6f8b36437 AS build
ARG TARGETARCH
ARG VERSION=dev
ARG TAGS=
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} \
    go build -trimpath -tags "${TAGS}" -ldflags "-s -w -X main.version=${VERSION}" -o /out/ ./cmd/...

FROM alpine:3.22.2
RUN adduser -D -u 10001 edge
COPY --from=build /out/ /usr/local/bin/
USER 10001
ENTRYPOINT ["/usr/local/bin/edge-gw"]
