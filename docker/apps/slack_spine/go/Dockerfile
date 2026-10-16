ARG TARGETARCH
ARG UPSTREAM_IMAGE=ghcr.io/abundant-ai/sre-world/slack-go@sha256:a9c60cefb4326ff918867ab522e05a9570b5b50fccc6208372228ed341d123dd

FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS original

FROM --platform=$BUILDPLATFORM golang:1.23.12-bookworm@sha256:167053a2bb901972bf2c1611f8f52c44d5fe7e762e5cab213708d82c421614db AS build
ARG TARGETARCH
WORKDIR /src
COPY --from=go-source go.mod go.sum ./
RUN go mod download
COPY --from=go-source internal ./internal
COPY --from=go-source cmd ./cmd
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -trimpath -o /out/ ./cmd/...

FROM scratch AS binaries
COPY --from=build /out/ /

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab AS runtime-arm64
COPY --from=build /out/ /usr/local/bin/

FROM original AS runtime-amd64

FROM runtime-${TARGETARCH}
