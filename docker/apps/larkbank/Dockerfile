# Larkbank, an executable approximation of the bank in Monzo's 29 July 2019
# incident: customers' payees and standing orders kept in a Cassandra ring,
# and a scheduler that pays the standing orders.
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
# carries it (build with --target hidden): the payment rail, the customers,
# and the reference repair.
FROM alpine:3.22.2 AS hidden
COPY --from=build /out/world /usr/local/bin/world
COPY --from=build /out/repair /usr/local/bin/repair
ENTRYPOINT ["/usr/local/bin/world"]

# The customer API and the scheduler.
FROM alpine:3.22.2
RUN apk add --no-cache curl && adduser -D -u 10001 larkbank
COPY --from=build /out/api /usr/local/bin/api
COPY --from=build /out/scheduler /usr/local/bin/scheduler
USER 10001
ENTRYPOINT ["/usr/local/bin/api"]
