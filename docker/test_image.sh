#!/usr/bin/env bash
# Check the selected platform before an image is used by a deployment.
set -euo pipefail
TARGET="${1:?Usage: test_image.sh TARGET IMAGE ARCH}"
IMAGE="${2:?Image reference is required}"
ARCH="${3:?Architecture is required}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

actual="$(docker image inspect --platform "linux/$ARCH" "$IMAGE" --format '{{.Os}}/{{.Architecture}}')"
if [[ "$actual" != "linux/$ARCH" ]]; then
    echo "Expected linux/$ARCH, got $actual for $IMAGE" >&2
    exit 1
fi

run_image() {
    docker run --rm --platform "linux/$ARCH" "$@"
}

case "$TARGET" in
    hotel-geo-misconfig)
        run_image --network none --entrypoint sh "$IMAGE" -ec '
            grep -q '\''"GeoMongoAddress": "mongodb-geo:27777"'\'' config.json
            status=0
            output="$(timeout 30 geo 2>&1)" || status=$?
            test "$status" -eq 2
            printf "%s\n" "$output" | grep "Read database URL: mongodb-geo:27777"
            printf "%s\n" "$output" | grep "panic: no reachable servers"
        '
        ;;
    hotel-correlated-fault)
        run_image --network none --entrypoint sh "$IMAGE" -ec '
            test -x /usr/local/bin/ComposePostService
            for service in frontend geo profile rate recommendation reservation search user; do
                status=0
                "$service" 2>/dev/null || status=$?
                test "$status" -eq 127
                ! command -v "$service"
            done
        '
        ;;
    stress)
        run_image --network none --entrypoint stress "$IMAGE" --version | grep -Fx 'stress 1.0.4'
        run_image --network none --cpus 1 --memory 64m --entrypoint stress "$IMAGE" --cpu 1 --timeout 1
        ;;
    flight-ticket-python-runtime)
        run_image -i --entrypoint python "$IMAGE" < "$SCRIPT_DIR/flight-ticket/test_python_runtime.py"
        ;;
    train-ticket-xenon)
        run_image --entrypoint xenoncli "$IMAGE" version
        ;;
    train-ticket-deploy)
        # Never execute deploy.sh here: it mutates the configured cluster.
        run_image --entrypoint bash "$IMAGE" -ec '
            helm version --short
            kubectl version --client
            mysql --version
            bash -n /usr/local/bin/deploy.sh
            cd /usr/local/bin/deployment/kubernetes-manifests/quickstart-k8s/charts
            for chart in mysql nacos rabbitmq; do
                helm template smoke "$chart" --namespace train-ticket >/dev/null
            done
            grep -q "ghcr.io/sregym/train-ticket-percona" mysql/values.yaml
            grep -q "ghcr.io/sregym/train-ticket-nacos" nacos/values.yaml
        '
        ;;
    train-ticket-nacos)
        run_image --entrypoint bash "$IMAGE" -ec '
            java -version
            bash -n /home/nacos/bin/docker-startup.sh
            test -s /home/nacos/target/nacos-server.jar
        '
        ;;
    train-ticket-mysqlclient)
        run_image --entrypoint sh "$IMAGE" -ec '
            mysql --version
            sh -n /init/init.sh /with-wait.sh
            test -s /init/nacos-mysql.sql
        '
        ;;
    train-ticket-mysqld-exporter)
        run_image "$IMAGE" --version
        ;;
    train-ticket-percona|train-ticket-alertsnitch-mysql)
        # Exercise containerd's large inherited limit under the chart's 1 GiB
        # budget, including the wrapper rather than just the server binary.
        run_image --memory 1g --ulimit nofile=1073741816:1073741816 \
            --entrypoint /usr/local/bin/mysql-nofile-entrypoint.sh "$IMAGE" \
            mysqld --verbose --help --log-bin-index=/tmp/sregym-smoke.index >/dev/null
        run_image --entrypoint sh "$IMAGE" -ec '
            mysqld --version
            mysql --version
            output="$(ldd "$(command -v mysqld)")"
            if printf "%s\n" "$output" | grep -q "not found"; then
                printf "%s\n" "$output" >&2
                exit 1
            fi
        '
        ;;
    flight-ticket-action-deployer)
        run_image --entrypoint bash "$IMAGE" -ec 'wsk --help >/dev/null; test -x /app/deploy_ow_actions.sh; test -d /app/actions'
        ;;
    flight-ticket-load-generator)
        run_image --entrypoint bash "$IMAGE" -ec 'wsk --help >/dev/null; python -m py_compile /app/run-all.py; test -x /app/entrypoint.sh'
        ;;
    flight-ticket-populate-redis)
        run_image --entrypoint python "$IMAGE" -c 'import redis, pandas'
        ;;
    fleetcast-backend)
        run_image --entrypoint python "$IMAGE" -c '
import server
from importlib.metadata import version
assert version("PyMySQL") == "1.1.2"
assert any(route.path == "/api/health" for route in server.app.routes)
'
        ;;
    kind-node)
        run_image --entrypoint sh "$IMAGE" -ec '
            kubelet --version
            command -v udevadm
            command -v socat
        '
        ;;
    agent-base)
        run_image --entrypoint bash "$IMAGE" -ec '
            kubectl version --client
            node --version
            python3 -c "import clients, logger, llm_backend; from sregym.service.kubectl import KubeCtl"
            test -x /opt/sregym/install-scripts/install-codex.sh
        '
        ;;
    hotel-reservation)
        run_image --entrypoint sh "$IMAGE" -ec '
            for service in frontend geo profile rate recommendation reservation search user; do
                test -x "/go/bin/$service"
            done
            go version
        '
        ;;
    locust-exporter)
        run_image "$IMAGE" --version
        ;;
    wrk2)
        run_image --entrypoint sh "$IMAGE" -ec '
            # This fork exits 1 after printing version/help without a URL.
            output="$(wrk --version 2>&1)" || test "$?" -eq 1
            printf "%s\n" "$output" | grep -- "--dist"
            test -d /usr/local/lib/lua/5.1/socket
        '
        ;;
    social-network-deps)
        run_image --entrypoint sh "$IMAGE" -ec 'thrift --version; test -f /usr/local/lib/libthrift.so'
        ;;
    social-network)
        run_image --entrypoint sh "$IMAGE" -ec '
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
        run_image --entrypoint /usr/local/openresty/bin/openresty "$IMAGE" -t
        if [[ "$TARGET" == media-frontend ]]; then
            run_image --entrypoint /usr/local/openresty/bin/resty "$IMAGE" -e '
                require "resty-mongol"; require "socket"; require "chronos"; require "magick"
            '
        fi
        ;;
    *) echo "No smoke test for $TARGET" >&2; exit 1 ;;
esac
