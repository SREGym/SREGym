# Match polinux/stress's stress 1.0.4 CPU worker on both architectures.
FROM alpine:3.22 AS build
RUN apk add --no-cache build-base curl autoconf automake
WORKDIR /src
RUN curl -fsSL https://deb.debian.org/debian/pool/main/s/stress/stress_1.0.4.orig.tar.gz -o /tmp/stress.tar.gz \
    && echo "057e4fc2a7706411e1014bf172e4f94b63a12f18412378fca8684ca92408825b  /tmp/stress.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/stress.tar.gz --strip-components=1 \
    && autoreconf -fi \
    && ./configure \
    && make -C src -j2 \
    && make -C src DESTDIR=/out install

FROM alpine:3.22
COPY --from=build /out/usr/local/bin/stress /usr/local/bin/stress
COPY --from=build /src/COPYING /usr/share/licenses/stress/COPYING
CMD ["/usr/local/bin/stress"]
