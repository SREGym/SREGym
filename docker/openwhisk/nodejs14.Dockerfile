ARG TARGETARCH
FROM --platform=linux/amd64 openwhisk/action-nodejs-v14:1.20.0@sha256:7fde2caf2e107e62da66dba387c79df8d1efd03eb515086d365d537aa9478f9f AS runtime-amd64
FROM node:14.19-stretch@sha256:ce156f9b2e9dbe73139cf0619a71188960e6c9eaba0ff832a5dfa0febf9eee27 AS runtime-arm64
# Match the upstream runtime's Debian release, now hosted in the archive.
RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g; s|security.debian.org/debian-security|archive.debian.org/debian-security|g; /stretch-updates/d' /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends imagemagick graphicsmagick unzip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=runtime-amd64 /nodejsAction/ /nodejsAction/
COPY --from=runtime-amd64 /package.json /package.json
COPY --from=runtime-amd64 /node_modules/ /node_modules/
RUN cd / && npm rebuild --production && npm cache clean --force
WORKDIR /nodejsAction
EXPOSE 8080
CMD ["/bin/sh", "-c", "node --expose-gc app.js"]
FROM runtime-${TARGETARCH}
