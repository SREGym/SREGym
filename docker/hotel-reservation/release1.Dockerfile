# Preserve yinfangchen/geo:app3 exactly on AMD64 and rebuild its embedded
# source/vendor tree on ARM64. Its fault is GeoMongoAddress's port 27777,
# not a different Geo algorithm; keep the original config.json unchanged.
ARG TARGETARCH
FROM --platform=linux/amd64 yinfangchen/geo@sha256:bc56ea15136b8d1330e9433bd7cba225882e635e5e4ee071be6ad9510032bb39 AS runtime-amd64

FROM golang:1.17.3 AS runtime-arm64
WORKDIR /go/src/github.com/harlow/go-micro-services
COPY --from=runtime-amd64 /go/src/github.com/harlow/go-micro-services/ ./
# All dependencies are in the original image: no module upgrades or network
# resolution, including its locally modified go-geoindex implementation.
RUN GOPROXY=off go install -mod=vendor -ldflags="-s -w" ./cmd/...

FROM runtime-${TARGETARCH}
