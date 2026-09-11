#!/usr/bin/env bash
# Run architecture-native smoke checks before an image can be published.
set -euo pipefail
TARGET="${1:?Usage: test_lite_image.sh TARGET IMAGE ARCH}"
IMAGE="${2:?Image reference is required}"
ARCH="${3:?Architecture is required}"

actual="$(docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}')"
if [[ "$actual" != "linux/$ARCH" ]]; then
    echo "Expected linux/$ARCH, got $actual for $IMAGE" >&2
    exit 1
fi

case "$TARGET" in
    hotel-reservation)
        docker run --rm --entrypoint sh "$IMAGE" -ec '
            for service in frontend geo profile rate recommendation reservation search user; do
                test -x "/go/bin/$service"
            done
            go version
        '
        ;;
    locust-exporter)
        docker run --rm "$IMAGE" --version
        ;;
    wrk2)
        docker run --rm --entrypoint sh "$IMAGE" -ec '
            # This fork exits 1 after printing version/help without a URL.
            output="$(wrk --version 2>&1)" || test "$?" -eq 1
            printf "%s\n" "$output" | grep -- "--dist"
            test -d /usr/local/lib/lua/5.1/socket
        '
        ;;
    social-network-deps)
        docker run --rm --entrypoint sh "$IMAGE" -ec 'thrift --version; test -f /usr/local/lib/libthrift.so'
        ;;
    social-network)
        docker run --rm --entrypoint sh "$IMAGE" -ec '
            for service in ComposePostService HomeTimelineService MediaService PostStorageService \
                SocialGraphService TextService UniqueIdService UrlShortenService UserMentionService \
                UserService UserTimelineService; do
                test -x "/usr/local/bin/$service"
                output="$(ldd "/usr/local/bin/$service")"
                if printf "%s\n" "$output" | grep -q "not found"; then
                    printf "%s\n" "$output" >&2
                    exit 1
                fi
            done
        '
        ;;
    openresty-thrift|media-frontend)
        docker run --rm --entrypoint /usr/local/openresty/bin/openresty "$IMAGE" -t
        ;;
    *) echo "No smoke test for $TARGET" >&2; exit 1 ;;
esac
