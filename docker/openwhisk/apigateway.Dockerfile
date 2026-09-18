ARG TARGETARCH
FROM --platform=linux/amd64 openwhisk/apigateway:1.0.0@sha256:df52384d7f9298874f96ef4c1006a01450cd37db4a4680e830bf9f231bd4ff00 AS runtime-amd64
FROM runtime-amd64 AS lua-sources
# Do not overwrite the rebuilt ARM Lua C modules (for example cjson.so).
RUN find /usr/local/api-gateway/lualib -type f ! -name '*.lua' -delete

FROM --platform=$BUILDPLATFORM golang:1.19.13-bullseye@sha256:2fdfcb03b1445f06f1cf8a342516bfd34026b527fef8427f40ea7b140168fda2 AS supervisor
ARG TARGETARCH
ADD --checksum=sha256:e91e63ce0a9bbac1ff2e205c2e237133af136484b1c958b9e85c60bccbeed3f8 https://api.github.com/repos/adobe-apiplatform/api-gateway-config-supervisor/tarball/e25d4513e400c9864b4c7c338b09108e09db7328 /tmp/supervisor.tar.gz
WORKDIR /go/src/github.com/adobe-apiplatform/api-gateway-config-supervisor
RUN tar -xzf /tmp/supervisor.tar.gz --strip-components=1 \
    && CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} GO111MODULE=off go build -trimpath -o /api-gateway-config-supervisor .

FROM alpine:3.9@sha256:414e0518bb9228d35e4cd5165567fb91d26c6a214e9c95899e1e056fcd349011 AS gateway-build
RUN apk add --no-cache build-base perl openssl-dev pcre-dev zlib-dev geoip-dev jansson-dev
ADD --checksum=sha256:946e1958273032db43833982e2cec0766154a9b5cb8e67868944113208ff2942 https://openresty.org/download/openresty-1.13.6.2.tar.gz /tmp/openresty.tar.gz
# Retain upstream's ARM ngx_lua port. LuaJIT also needs full-range lightuserdata
# support on 48-bit ARM kernels (LuaJIT/LuaJIT commit e9af1abec542e6f9851ff2368e7f196b6382a44c).
ADD --checksum=sha256:ee35f3f1d9821ce31cab8f8dfd9f2c1b8e5497bdb3bc3fa73724141a704ea484 https://api.github.com/repos/openresty/luajit2/tarball/v2.1-20211210 /tmp/luajit.tar.gz
ADD --checksum=sha256:cd2e111688ab898bfc042343d17e6ccb62d955e69a389ad48c89fab6550aa0ee https://api.github.com/repos/openresty/lua-nginx-module/tarball/v0.10.14rc3 /tmp/ngx-lua.tar.gz
ADD --checksum=sha256:e7828c48a373f1b25237af7d6553a202eb3a30e30aae3de98857d42fe3a4f0db https://api.github.com/repos/cisco/cjose/tarball/0.5.1 /tmp/cjose.tar.gz
COPY build-gateway.sh /tmp/build-gateway.sh
RUN sh /tmp/build-gateway.sh

FROM rclone/rclone:1.60.1@sha256:895e89550af5f00e2b3d0b2849a8caa0e17a99b8e57d6e1dac7ecb07289e71a3 AS rclone
FROM alpine:3.9@sha256:414e0518bb9228d35e4cd5165567fb91d26c6a214e9c95899e1e056fcd349011 AS runtime-arm64
RUN apk add --no-cache bash curl openssl pcre zlib geoip jansson libgcc libstdc++ dumb-init jq perl \
    && addgroup -S nginx-api-gateway && adduser -S -G nginx-api-gateway nginx-api-gateway \
    && mkdir -p /var/log/api-gateway /var/run /root/.config/rclone
COPY --from=gateway-build /usr/local/ /usr/local/
# Lua code and gateway configuration are portable; compiled components come only
# from the native build, never from the AMD64 image.
COPY --from=lua-sources /usr/local/api-gateway/lualib/ /usr/local/api-gateway/lualib/
COPY --from=runtime-amd64 /etc/api-gateway/ /etc/api-gateway/
COPY --from=runtime-amd64 /etc/init-container.sh /etc/init-container.sh
COPY --from=runtime-amd64 /root/.config/rclone/ /root/.config/rclone/
COPY --from=supervisor /api-gateway-config-supervisor /usr/local/sbin/
COPY --from=rclone /usr/local/bin/rclone /usr/local/sbin/rclone
ENV LD_LIBRARY_PATH=/usr/local/lib
EXPOSE 80 8080 8423 9000
ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/etc/init-container.sh"]
FROM runtime-${TARGETARCH}
