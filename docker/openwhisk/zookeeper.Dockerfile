ARG TARGETARCH
FROM --platform=linux/amd64 zookeeper:3.4@sha256:3882d9493d387ba77b7b69e2a031b9396477ec29483d51ceaed645c1389182e5 AS runtime-amd64
FROM eclipse-temurin:8-jre-jammy@sha256:06641b36281c1ac815c33f3f3528cfea1c6fc41ddc60d261746e4343d19cbe65 AS runtime-arm64
RUN apt-get update && apt-get install -y --no-install-recommends bash gosu netcat-openbsd \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 zookeeper && useradd --uid 1000 --gid zookeeper zookeeper \
    && mkdir /data /datalog /logs /conf \
    && chown zookeeper:zookeeper /data /datalog /logs /conf
COPY --from=runtime-amd64 /zookeeper-3.4.14/ /zookeeper-3.4.14/
COPY --from=runtime-amd64 /conf/ /conf/
COPY --from=runtime-amd64 /docker-entrypoint.sh /
ENV PATH="/zookeeper-3.4.14/bin:${PATH}" \
    ZOO_CONF_DIR=/conf ZOOCFGDIR=/conf ZOO_DATA_DIR=/data \
    ZOO_DATA_LOG_DIR=/datalog ZOO_LOG_DIR=/logs ZOO_TICK_TIME=2000 \
    ZOO_INIT_LIMIT=5 ZOO_SYNC_LIMIT=2 ZOO_AUTOPURGE_PURGEINTERVAL=0 \
    ZOO_AUTOPURGE_SNAPRETAINCOUNT=3 ZOO_MAX_CLIENT_CNXNS=60
WORKDIR /zookeeper-3.4.14
VOLUME ["/data", "/datalog", "/logs"]
EXPOSE 2181 2888 3888
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["zkServer.sh", "start-foreground"]
FROM runtime-${TARGETARCH}
