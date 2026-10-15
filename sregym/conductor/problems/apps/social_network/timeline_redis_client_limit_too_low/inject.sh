#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}/common.sh"
mkdir -p "$ARTIFACT_DIR/state"
stop_publisher
if [[ ! -f "$ARTIFACT_DIR/state/maxclients" ]]; then
  get_client_budget > "$ARTIFACT_DIR/state/maxclients.response"
  python3 - "$ARTIFACT_DIR/state/maxclients.response" "$ARTIFACT_DIR/state/maxclients" <<'PY'
import pathlib, sys
lines = pathlib.Path(sys.argv[1]).read_text().splitlines()
assert len(lines) == 2 and lines[0] == 'maxclients' and lines[1].isdigit(), lines
pathlib.Path(sys.argv[2]).write_text(lines[1] + '\n')
PY
fi
set_client_budget "$(cat "$ARTIFACT_DIR/state/maxclients")"
kubectl exec -i -n "$NAMESPACE" deploy/media-mongodb -- bash -c 'cat > /tmp/cache-session-profile.sh; chmod 700 /tmp/cache-session-profile.sh' < "$ARTIFACT_DIR/cache-session-profile.sh"
replica_count=$(kubectl get -n "$NAMESPACE" deploy/user-timeline-redis -o jsonpath='{.spec.replicas}')
kubectl exec -i -n "$NAMESPACE" deploy/media-mongodb -- bash -s -- "user-timeline-redis.$NAMESPACE.svc.cluster.local" "$replica_count" <<'SH'
set -euo pipefail
ready=/tmp/cache-session-profile.ready
mkfifo "$ready"
exec 8<>"$ready"
nohup bash /tmp/cache-session-profile.sh "$1" "$ready" "$2" > /tmp/cache-session-profile.log 2>&1 < /dev/null &
process=$!
printf '%s\n' "$process" > /tmp/cache-session-profile.pid
if ! IFS= read -r -t 45 status <&8 || [[ $status != ready ]]; then
  cat /tmp/cache-session-profile.log >&2
  kill -KILL "$process" 2>/dev/null || true
  exit 1
fi
exec 8>&-
rm -f "$ready"
kill -0 "$process"
SH
printf '%s\n' 'Cache session profile is active.'
