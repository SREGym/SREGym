ARG TARGETARCH
FROM --platform=linux/amd64 openwhisk/alarmprovider:2.3.0@sha256:92f55f402e07b83939dfc65d17f1ef56e5126054efe853469c6ce79a14e417c4 AS runtime-amd64
FROM node:14.17.2-buster@sha256:399fc1df21b475c08714d4f8f12bddf01f3ce3ae330ef7a39b03fdb986d4e2dc AS runtime-arm64
# Preserve the installed dependency versions; rebuild any native Node addons.
COPY --from=runtime-amd64 /alarmsTrigger/ /alarmsTrigger/
COPY --from=runtime-amd64 /package.json /package-lock.json /
COPY --from=runtime-amd64 /node_modules/ /node_modules/
RUN cd / && npm rebuild --production && npm cache clean --force
EXPOSE 8080
CMD ["/bin/bash", "-c", "node /alarmsTrigger/app.js"]
FROM runtime-${TARGETARCH}
