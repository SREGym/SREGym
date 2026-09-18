# Alertsnitch's original image is MySQL 5.7.36 plus two initialization SQL
# files. Keep AMD64 binaries and compile that MySQL release for ARM64.
ARG TARGETARCH
FROM --platform=linux/amd64 569107519/alertsnitch-mysql@sha256:e3c1b2c8c8816e8f050a7c20008716eec21c58d6a577e1c018e2b24106ed06c8 AS runtime-amd64

FROM ubuntu:20.04 AS build-arm64
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl cmake make g++ bison pkg-config \
      libssl-dev libncurses5-dev libaio-dev zlib1g-dev libnuma-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# Official mysql-5.7.36 source revision.
RUN curl -fsSL https://github.com/mysql/mysql-server/archive/0ed6d65f4c60a38e77a672fc528efd3f44bc7701.tar.gz \
      | tar -xz --strip-components=1 \
    && mkdir /boost \
    && curl -fsSL https://archives.boost.io/release/1.59.0/source/boost_1_59_0.tar.bz2 \
      | tar -xj -C /boost --strip-components=1
RUN cmake -S . -B /build \
      -DCMAKE_INSTALL_PREFIX=/usr -DINSTALL_LAYOUT=DEB \
      -DMYSQL_UNIX_ADDR=/var/run/mysqld/mysqld.sock \
      -DCMAKE_BUILD_TYPE=Release -DWITH_BOOST=/boost -DDOWNLOAD_BOOST=OFF \
      -DWITH_SSL=system -DWITH_UNIT_TESTS=OFF \
    && cmake --build /build --parallel 2 \
    && DESTDIR=/install cmake --install /build

FROM ubuntu:20.04 AS runtime-arm64
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates libaio1 libssl1.1 libncurses5 libnuma1 libatomic1 \
      perl openssl gosu tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 999 --system mysql \
    && useradd --uid 999 --system --home-dir /var/lib/mysql --no-create-home --gid mysql mysql
COPY --from=build-arm64 /install/ /
COPY --from=runtime-amd64 /usr/local/bin/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --from=runtime-amd64 /etc/mysql/ /etc/mysql/
COPY --from=runtime-amd64 /docker-entrypoint-initdb.d/ /docker-entrypoint-initdb.d/
RUN ln -sf /etc/mysql/mysql.cnf /etc/mysql/my.cnf \
    && mkdir -p /var/lib/mysql /var/lib/mysql-files /var/run/mysqld \
    && chown -R mysql:mysql /var/lib/mysql /var/lib/mysql-files /var/run/mysqld \
    && chmod 750 /var/lib/mysql-files \
    && chmod 1777 /var/run/mysqld /var/lib/mysql
ENV MYSQL_MAJOR=5.7 MYSQL_VERSION=5.7.36
VOLUME ["/var/lib/mysql"]
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["mysqld"]
EXPOSE 3306 33060

FROM runtime-${TARGETARCH}
COPY --chmod=755 mysql-nofile-entrypoint.sh /usr/local/bin/mysql-nofile-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/mysql-nofile-entrypoint.sh", "docker-entrypoint.sh"]
CMD ["mysqld"]
