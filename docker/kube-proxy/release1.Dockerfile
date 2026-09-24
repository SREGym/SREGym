# AMD64 retains the original benchmark image. ARM64 rebuilds Kubernetes 1.31.12
# with the same constant-probability behavior recovered from that binary.
ARG TARGETARCH
FROM --platform=linux/amd64 jackcuii/kube-proxy@sha256:1766fa7073685b463499b85c5643c2724c28568ece1c771a7b1b60dd9d70de39 AS runtime-amd64

FROM --platform=$BUILDPLATFORM golang:1.23.11-bookworm@sha256:3860392b86f6b41bb9290534038444a3e3e3bb63c499f11f36ff9864fd6f7f58 AS build
ADD --checksum=sha256:b22a6f1db3457acc0e0e821061938e16a8bd8f0274377e62482560756f48689d https://github.com/kubernetes/kubernetes/archive/c1e5f4a23e5ff5587504fd75d2ab828ed7d0d373.tar.gz /tmp/kubernetes.tar.gz
WORKDIR /src
RUN tar -xzf /tmp/kubernetes.tar.gz --strip-components=1
COPY probability.patch /tmp/probability.patch
RUN git apply --check /tmp/probability.patch && git apply /tmp/probability.patch
ARG TARGETARCH
RUN CGO_ENABLED=0 GOOS=linux GOARCH=$TARGETARCH GOMAXPROCS=4 \
    go build -p 4 -mod=vendor -trimpath -tags=selinux,notest \
    -ldflags='-s -w -X k8s.io/component-base/version.gitVersion=v1.31.12' \
    -o /out/kube-proxy ./cmd/kube-proxy

FROM registry.k8s.io/kube-proxy:v1.31.12@sha256:90aa6b5f4065937521ff8438bc705317485d0be3f8b00a07145e697d92cc2cc6 AS runtime-arm64
COPY --from=build /out/kube-proxy /usr/local/bin/kube-proxy

FROM runtime-${TARGETARCH}
