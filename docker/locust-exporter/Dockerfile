# Native replacement for the amd64-only exporter pinned by the demo chart.
FROM --platform=$BUILDPLATFORM golang:1.24-alpine AS builder

ARG TARGETOS
ARG TARGETARCH
ARG LOCUST_EXPORTER_REF=c51c891952b358a2fad7d8495ccc466e414bb399

RUN apk add --no-cache git
WORKDIR /src
RUN git clone https://github.com/ContainerSolutions/locust_exporter.git . \
    && git checkout "${LOCUST_EXPORTER_REF}"
RUN CGO_ENABLED=0 GOOS="${TARGETOS:-linux}" GOARCH="${TARGETARCH}" \
    go build -trimpath -tags netgo -ldflags="-s -w" -o /out/locust_exporter .

FROM scratch

EXPOSE 9646
USER 1000
COPY --from=builder /out/locust_exporter /bin/locust_exporter
ENTRYPOINT ["/bin/locust_exporter"]
