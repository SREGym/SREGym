FROM --platform=$BUILDPLATFORM golang:1.26.3-bookworm@sha256:386d475a660466863d9f8c766fec64d7fdad3edac2c6a05020c09534d71edb4b AS build
ARG TARGETOS TARGETARCH
WORKDIR /src
RUN git init -q . && git remote add origin https://github.com/mattermost/mattermost-push-proxy.git \
 && git fetch -q --depth 1 origin 76bc7f53a30a7214e1e1a41659696ca71837847b && git checkout -q FETCH_HEAD
RUN sed -i 's|app, err := firebase.NewApp(context.Background(), conf, opt)|app, err := firebase.NewApp(context.Background(), conf, opt, option.WithEndpoint(os.Getenv("FCM_ENDPOINT")))|' server/android_notification_server.go \
 && test "$(grep -c 'option.WithEndpoint(os.Getenv' server/android_notification_server.go)" = 1
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -o /out/mattermost-push-proxy \
 -ldflags "-X github.com/mattermost/mattermost-push-proxy/internal/version.gitVersion=v6.5.0 \
 -X github.com/mattermost/mattermost-push-proxy/internal/version.buildHash=76bc7f5" .

FROM ubuntu:noble-20250529
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/mattermost-push-proxy /mattermost-push-proxy/bin/mattermost-push-proxy
RUN mkdir -p /mattermost-push-proxy/config /mattermost-push-proxy/certs && chown -R nobody:nogroup /mattermost-push-proxy
USER nobody
WORKDIR /mattermost-push-proxy
EXPOSE 8066
ENTRYPOINT ["/mattermost-push-proxy/bin/mattermost-push-proxy"]
