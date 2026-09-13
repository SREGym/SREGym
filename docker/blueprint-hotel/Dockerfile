# AMD64 keeps the original benchmark binaries; ARM64 is built from pinned source
# with the observed RPC and workload behavior restored by compatibility.patch.
ARG TARGETARCH
ARG ORIGINAL_IMAGE
FROM --platform=linux/amd64 ${ORIGINAL_IMAGE} AS original

FROM --platform=$BUILDPLATFORM golang:1.23.12-bookworm@sha256:167053a2bb901972bf2c1611f8f52c44d5fe7e762e5cab213708d82c421614db AS generator
RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler=3.21.12-3+deb12u1 && rm -rf /var/lib/apt/lists/*
RUN go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.8 && \
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.5.1
ADD --checksum=sha256:55551c07dff5020382ce20ba27adecff5a2a5dc03bab38b3fc91a67a8f31bcae https://github.com/jq-huang/blueprint/archive/15c2f0c24ab6c51f08cd625644feb09af33a7c9a.tar.gz /tmp/blueprint.tar.gz
WORKDIR /src
RUN tar -xzf /tmp/blueprint.tar.gz --strip-components=1
COPY compatibility.patch /tmp/compatibility.patch
RUN git apply --check /tmp/compatibility.patch && git apply /tmp/compatibility.patch
ENV GOMAXPROCS=4 GOFLAGS=-p=4 GOTOOLCHAIN=local
WORKDIR /src/examples/dsb_hotel/wiring
RUN go run main.go -w original -o /generated

FROM generator AS service-build
ARG SERVICE
ARG TARGETARCH
COPY service-dependencies.txt /tmp/service-dependencies.txt
WORKDIR /generated/docker/${SERVICE}_service_container/${SERVICE}_service_process/${SERVICE}_service_process
RUN while IFS= read -r dependency; do go mod edit "-require=$dependency"; done < /tmp/service-dependencies.txt && \
    go mod tidy && \
    CGO_ENABLED=0 GOOS=linux GOARCH=$TARGETARCH go build -trimpath -o /out/service .

FROM --platform=$BUILDPLATFORM golang:1.25.0-bookworm@sha256:81dc45d05a7444ead8c92a389621fafabc8e40f8fd1a19d7e5df14e61e98bc1a AS workload-build
COPY --from=generator /generated/wlgen/wlgen_proc /build
WORKDIR /build/wlgen_proc
ARG TARGETARCH
RUN CGO_ENABLED=0 GOOS=linux GOARCH=$TARGETARCH GOMAXPROCS=4 GOTOOLCHAIN=local \
    go build -p 4 -trimpath -o /out/wlgen_proc .

FROM busybox:1.35.0-uclibc@sha256:e5877866756a3714cd11309f0b34e7c2c62406fdbfcae39c96beabb5dd4005a7 AS shell
FROM gcr.io/distroless/base-debian12@sha256:fabbf1c0c357a3d42550111351daed089b20a2c954df13ee2fcff60602515e84 AS runtime
COPY --from=shell /bin/sh /bin/sh
COPY --from=generator /src/LICENSE /usr/share/licenses/blueprint/LICENSE

FROM runtime AS service-runtime-arm64
ARG SERVICE
COPY --from=service-build /out/service /${SERVICE}_service_process/${SERVICE}_service_process
COPY --from=generator /generated/docker/${SERVICE}_service_container/run.sh /run.sh
WORKDIR /
ENTRYPOINT ["/bin/sh", "/run.sh"]

FROM original AS service-runtime-amd64
COPY --from=generator /src/LICENSE /usr/share/licenses/blueprint/LICENSE
FROM service-runtime-${TARGETARCH} AS service

FROM runtime AS workload-runtime-arm64
COPY --from=workload-build /out/wlgen_proc /app/wlgen_proc
WORKDIR /app
CMD ["/app/wlgen_proc"]

FROM original AS workload-runtime-amd64
COPY --from=generator /src/LICENSE /usr/share/licenses/blueprint/LICENSE
FROM workload-runtime-${TARGETARCH} AS workload
