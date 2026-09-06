#!/usr/bin/env bash
# Run on native AMD64 or ARM64 Linux, including Docker Desktop on macOS.
set -euo pipefail

kind=${1:?Usage: smoke-test.sh redis|kafka|tls IMAGE amd64|arm64}
image=${2:?An image reference is required}
arch=${3:?An architecture is required}

case "$arch" in
  amd64) machine=x86_64 ;;
  arm64) machine=aarch64 ;;
  *) echo "Unsupported architecture: $arch" >&2; exit 2 ;;
esac

test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")" = "linux/$arch"
test "$(docker run --rm --network none "$image" uname -m)" = "$machine"

case "$kind" in
  redis)
    docker run --rm --network none "$image" python3 -c \
      'import redis; assert redis.__version__ == "8.1.0"; redis.Redis(host="127.0.0.1"); print(redis.__version__)'
    ;;
  kafka)
    docker run --rm --network none "$image" python3 -c \
      'import confluent_kafka as k; assert k.__version__ == "2.5.3"; assert k.libversion()[0] == "2.5.3"; k.Producer({"bootstrap.servers": "127.0.0.1:1", "log_level": 0}); k.Consumer({"group.id": "smoke"}).close(); print(k.libversion())'
    ;;
  tls)
    docker run --rm --network none "$image" sh -ec '
      test -s /etc/ssl/certs/ca-certificates.crt
      openssl req -x509 -newkey rsa:2048 -nodes -keyout /tmp/key.pem \
        -out /tmp/cert.pem -days 1 -subj /CN=localhost >/dev/null 2>&1
      openssl verify -CAfile /tmp/cert.pem /tmp/cert.pem
      if openssl verify -attime "$(($(date +%s) + 2592000))" \
        -CAfile /tmp/cert.pem /tmp/cert.pem >/tmp/expired.log 2>&1; then
        exit 1
      fi
      grep -q "certificate has expired" /tmp/expired.log
      openssl version
    '
    ;;
  *) echo "Unknown runtime image: $kind" >&2; exit 2 ;;
esac
