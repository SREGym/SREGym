# Experimental: a multi-node k3s cluster that runs inside ONE unprivileged container.
# See README.md for the required (non-privileged) security options.
FROM nicolaka/netshoot@sha256:b09d9b21381f47a79b3cbcb30da25266dc17186ea00ae65e99fdc51396f48e70 AS tools

FROM rancher/k3s:v1.32.5-k3s1@sha256:b75eac801bbc91b4584769c840f346683c7740d72f83e630088370f8e1eb2c8a
# util-linux unshare/nsenter, iproute2 and socat (musl builds plus their libraries).
COPY --from=tools /usr/bin/unshare /usr/bin/nsenter /usr/bin/socat /usr/local/bin/
COPY --from=tools /sbin/ip /usr/local/bin/ip
COPY --from=tools /lib/ld-musl-x86_64.so.1 /lib/
COPY --from=tools /usr/lib/libelf.so.1 /usr/lib/libmnl.so.0 /usr/lib/libcap.so.2 \
    /usr/lib/libz.so.1 /usr/lib/libzstd.so.1 /usr/lib/libreadline.so.8 \
    /usr/lib/libssl.so.3 /usr/lib/libcrypto.so.3 /usr/lib/libncursesw.so.6 /usr/lib/
RUN mv /bin/runc /bin/runc.real
COPY runc-wrapper /bin/runc
COPY entrypoint.sh fabric.sh node.sh /usr/local/lib/userns-k3s/
ENV PATH=/usr/local/bin:$PATH
EXPOSE 6443
ENTRYPOINT ["/bin/sh", "/usr/local/lib/userns-k3s/entrypoint.sh"]
