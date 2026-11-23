# Tracker, an executable approximation of the issue tracker in Linear's
# 24 January 2024 incident: an API in front of PostgreSQL that records every
# change as a sync action, and the users whose apps follow them.
FROM --platform=$BUILDPLATFORM golang:1.25-bookworm@sha256:3b4a11519ad929d1e1d261a12cff056f0c85b735253d7d861346b9c6f8b36437 AS build
ARG TARGETARCH
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY internal ./internal
COPY cmd ./cmd
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} \
    go vet ./... && \
    CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags "-s -w" -o /out/ ./cmd/...

# The hidden side, in an image of its own so nothing the agent can reach
# carries it (build with --target hidden): Tracker's users, and the reference
# replay of the lost window.
FROM python:3.12-alpine3.22 AS hidden
COPY hidden/users.py hidden/replay.py /app/
ENTRYPOINT ["python3", "-u", "/app/users.py"]

# tracker-api.
FROM alpine:3.22.2
RUN apk add --no-cache curl && adduser -D -u 10001 tracker
COPY --from=build /out/api /usr/local/bin/tracker-api
USER 10001
ENTRYPOINT ["/usr/local/bin/tracker-api"]
