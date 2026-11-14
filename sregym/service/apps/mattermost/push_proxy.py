"""Mattermost's push relay, built from its v6.5.0 source with a configurable Firebase endpoint.

Upstream creates its Firebase messaging client with Google's default send
endpoint and no way to change it. This build adds one option to that call, so
the endpoint comes from ``FCM_ENDPOINT`` (empty keeps the default); nothing
else in the program changes, including its retry logging and its request
throttle. The runtime image follows upstream's Dockerfile.
"""

import json

from sregym.service.apps.mattermost.in_house import InHouseImage

#: Tag v6.5.0 of github.com/mattermost/mattermost-push-proxy.
SOURCE_COMMIT = "76bc7f53a30a7214e1e1a41659696ca71837847b"
VERSION = "6.5.0"
BUILD_IMAGE = "golang:1.26.3-bookworm@sha256:386d475a660466863d9f8c766fec64d7fdad3edac2c6a05020c09534d71edb4b"
RUNTIME_BASE = "ubuntu:noble-20250529"
CLIENT_CALL = "app, err := firebase.NewApp(context.Background(), conf, opt)"
ENDPOINT_CALL = (
    'app, err := firebase.NewApp(context.Background(), conf, opt, option.WithEndpoint(os.Getenv("FCM_ENDPOINT")))'
)
PORT = 8066

RECIPE = f"""FROM {BUILD_IMAGE} AS build
WORKDIR /src
RUN git init -q . && git remote add origin https://github.com/mattermost/mattermost-push-proxy.git \\
 && git fetch -q --depth 1 origin {SOURCE_COMMIT} && git checkout -q FETCH_HEAD
RUN sed -i 's|{CLIENT_CALL}|{ENDPOINT_CALL}|' server/android_notification_server.go \\
 && test "$(grep -c 'option.WithEndpoint(os.Getenv' server/android_notification_server.go)" = 1
RUN CGO_ENABLED=0 go build -trimpath -o /out/mattermost-push-proxy \\
 -ldflags "-X github.com/mattermost/mattermost-push-proxy/internal/version.gitVersion=v{VERSION} \\
 -X github.com/mattermost/mattermost-push-proxy/internal/version.buildHash={SOURCE_COMMIT[:7]}" .

FROM {RUNTIME_BASE}
RUN apt-get update \\
 && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y ca-certificates tzdata \\
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/mattermost-push-proxy /mattermost-push-proxy/bin/mattermost-push-proxy
RUN mkdir -p /mattermost-push-proxy/config /mattermost-push-proxy/certs && chown -R nobody:nogroup /mattermost-push-proxy
USER nobody
WORKDIR /mattermost-push-proxy
EXPOSE {PORT}
ENTRYPOINT ["/mattermost-push-proxy/bin/mattermost-push-proxy"]
"""


def image():
    return InHouseImage("mobile/push-proxy", {}, recipe=RECIPE)


def config(service_file="/mattermost-push-proxy/certs/firebase.json"):
    """The relay's configuration: Android through Firebase, no Apple apps, upstream's throttle defaults."""
    return json.dumps(
        {
            "ListenAddress": f":{PORT}",
            "ThrottlePerSec": 300,
            "ThrottleMemoryStoreSize": 50000,
            "ThrottleVaryByHeader": "X-Forwarded-For",
            "EnableMetrics": True,
            "SendTimeoutSec": 30,
            "RetryTimeoutSec": 8,
            "ApplePushSettings": [],
            "AndroidPushSettings": [{"Type": "android_rn", "ServiceFileLocation": service_file}],
            "EnableConsoleLog": True,
            "EnableFileLog": False,
            "LogFormat": "plain",
        },
        indent=2,
    )
