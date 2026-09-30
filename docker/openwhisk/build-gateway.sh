#!/bin/sh
# Build the native portions of OpenWhisk API Gateway 1.0.0. The portable Lua
# application and configuration are retained from its pinned release image.
set -eu

cd /tmp
tar -xzf openresty.tar.gz
cd openresty-1.13.6.2
rm -rf bundle/ngx_lua-0.10.13 bundle/LuaJIT-2.1-20180420
mkdir -p bundle/ngx_lua-0.10.14rc3 bundle/LuaJIT-2.1-20211210
tar -xzf /tmp/ngx-lua.tar.gz -C bundle/ngx_lua-0.10.14rc3 --strip-components=1
tar -xzf /tmp/luajit.tar.gz -C bundle/LuaJIT-2.1-20211210 --strip-components=1

for variant in debug release; do
    binary=api-gateway
    debug_flag=
    if [ "$variant" = debug ]; then
        binary=api-gateway-debug
        debug_flag=--with-debug
    fi
    ./configure --prefix=/usr/local/api-gateway \
        --sbin-path="/usr/local/sbin/$binary" \
        --conf-path=/etc/api-gateway/api-gateway.conf \
        --error-log-path=/var/log/api-gateway/error.log \
        --http-log-path=/var/log/api-gateway/access.log \
        --pid-path=/var/run/api-gateway.pid --lock-path=/var/run/api-gateway.lock \
        --with-pcre-jit --with-stream --with-stream_ssl_module \
        --with-http_ssl_module --with-http_stub_status_module \
        --with-http_realip_module --with-http_addition_module --with-http_sub_module \
        --with-http_dav_module --with-http_geoip_module --with-http_gunzip_module \
        --with-http_gzip_static_module --with-http_auth_request_module \
        --with-http_random_index_module --with-http_secure_link_module \
        --with-http_degradation_module --with-http_v2_module --with-luajit \
        --without-http_ssi_module --without-http_userid_module \
        --without-http_uwsgi_module --without-http_scgi_module ${debug_flag} -j2
    make -j2
    make install
done
ln -s /usr/local/sbin/api-gateway-debug /usr/local/sbin/nginx

mkdir /tmp/cjose
tar -xzf /tmp/cjose.tar.gz -C /tmp/cjose --strip-components=1
cd /tmp/cjose
./configure
make -j2
make install
