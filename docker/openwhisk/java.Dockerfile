# Keep the original JVM application and AMD64 image. Only the ARM64 JVM,
# operating-system tools, and Docker client are replaced with native binaries.
ARG TARGETARCH
ARG COMPONENT=controller
ARG UPSTREAM_IMAGE=openwhisk/controller@sha256:b2e86ffa03c826cb7808fe524be980549628c4d29ce73a40c6cd950578e1da9f
FROM --platform=linux/amd64 ${UPSTREAM_IMAGE} AS upstream

FROM eclipse-temurin:11-jre-jammy@sha256:4c01a3661ebf16e2213ce7ee8c6c8cca32ca0a27b33b5210f21d26d77b7d9cc8 AS java-arm64
ARG COMPONENT
RUN apt-get update && apt-get install -y --no-install-recommends bash curl docker.io openssl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1001 --create-home --shell /bin/bash owuser \
    && mkdir /logs \
    && ln -s /usr/sbin/runc /usr/bin/docker-runc
COPY --from=upstream /${COMPONENT}/ /${COMPONENT}/
COPY --from=upstream /init.sh /transformEnvironment.sh /copyJMXFiles.sh /
ENV LANG=C.UTF-8 UID=1001 NOT_ROOT_USER=owuser
WORKDIR /
ENTRYPOINT []
EXPOSE 8080
CMD ["./init.sh", "0"]

FROM java-arm64 AS controller-arm64
COPY --from=upstream /swagger-ui/ /swagger-ui/
FROM java-arm64 AS invoker-arm64
FROM upstream AS controller-amd64
FROM upstream AS invoker-amd64
FROM ${COMPONENT}-${TARGETARCH}
