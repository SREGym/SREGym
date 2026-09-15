# Preserve the existing AMD64 binaries. ARM64 builds the same Percona release
# from source, retaining RadonDB's configuration and initialization script.
ARG TARGETARCH
FROM --platform=linux/amd64 radondb/percona@sha256:3901a1cf63bc772a6132ece45bddbb16cfc491f8b5e2b2498ef6f37003c66cd0 AS runtime-amd64

FROM ubuntu:20.04 AS build-arm64
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl cmake make g++ bison pkg-config \
      libssl-dev libcurl4-openssl-dev libncurses5-dev libreadline-dev libaio-dev zlib1g-dev libnuma-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# Percona-Server-5.7.34-37, matching the deployed 5.7.34-37-1 packages.
RUN curl -fsSL https://github.com/percona/percona-server/archive/7c516e99cf2f61c804975b946c9a815f2c46875d.tar.gz \
      | tar -xz --strip-components=1 \
    && curl -fsSL https://github.com/Percona-Lab/coredumper/archive/e7f3a5447ddb1047c40028f41a45eb4cbf39c38a.tar.gz \
      | tar -xz -C extra/coredumper --strip-components=1 \
    && mkdir /boost \
    && curl -fsSL https://archives.boost.io/release/1.59.0/source/boost_1_59_0.tar.bz2 \
      | tar -xj -C /boost --strip-components=1
# Train Ticket uses InnoDB and semi-sync replication. TokuDB's x86-only
# engine is deliberately not included in the ARM64 variant.
RUN cmake -S . -B /build \
      -DCMAKE_INSTALL_PREFIX=/usr -DINSTALL_LAYOUT=DEB \
      -DMYSQL_UNIX_ADDR=/var/run/mysqld/mysqld.sock \
      -DCMAKE_BUILD_TYPE=Release -DWITH_BOOST=/boost -DDOWNLOAD_BOOST=OFF \
      -DWITH_SSL=system -DWITH_TOKUDB=OFF -DWITH_ROCKSDB=OFF \
      -DWITH_TOKUBACKUP=OFF -DWITH_UNIT_TESTS=OFF \
    && cmake --build /build --parallel 2 \
    && DESTDIR=/install cmake --install /build

FROM ubuntu:20.04 AS runtime-arm64
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates libaio1 libssl1.1 libcurl4 libncurses5 libreadline8 libnuma1 libatomic1 tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 999 --system mysql \
    && useradd --uid 999 --system --home-dir /var/lib/mysql --no-create-home --gid mysql mysql
COPY --from=build-arm64 /install/ /
COPY --from=runtime-amd64 /docker-entrypoint.sh /docker-entrypoint.sh
COPY --from=runtime-amd64 /etc/mysql/ /etc/mysql/
RUN mkdir -p /var/lib/mysql /var/lib/mysql-files /var/log/mysql /var/run/mysqld /etc/mysql/conf.d /docker-entrypoint-initdb.d \
    && chown -R mysql:mysql /var/lib/mysql /var/lib/mysql-files /var/log/mysql /var/run/mysqld /etc/mysql \
    && chmod 750 /var/lib/mysql-files \
    && chmod 1777 /var/run/mysqld
VOLUME ["/var/lib/mysql", "/var/log/mysql"]
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["mysqld"]
EXPOSE 3306

FROM runtime-${TARGETARCH}
COPY --chmod=755 mysql-nofile-entrypoint.sh /usr/local/bin/mysql-nofile-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/mysql-nofile-entrypoint.sh", "/docker-entrypoint.sh"]
CMD ["mysqld"]
