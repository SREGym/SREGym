#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-social-network}"

# Terminate the operational owner before repairing the published native setting.
# Inspect command arguments, rather than trusting a saved PID or local snapshot.
for pod in $(kubectl -n "$NAMESPACE" get pods -l service=media-frontend --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}'); do
  kubectl -n "$NAMESPACE" exec -i "$pod" -c media-frontend -- python - <<'PY'
import os, signal
own = os.getpid()
for name in os.listdir('/proc'):
    if not name.isdigit() or int(name) == own:
        continue
    try:
        parts = open('/proc/'+name+'/cmdline','rb').read().split(b'\0')
        if b'/tmp/cache-session-policy.py' in parts:
            os.kill(int(name), signal.SIGKILL)
    except (IOError, OSError):
        pass
for path in ('/tmp/cache-session-policy.py','/tmp/cache-session-policy.log'):
    try:
        os.unlink(path)
    except OSError:
        pass
PY
done

# The stock Redis normal-client limit is unlimited. Replica and pubsub limits
# are the Redis 6.2 defaults; neither cache has replication or subscriptions.
for service in user-timeline-redis home-timeline-redis; do
  result="$(kubectl -n "$NAMESPACE" exec "deployment/$service" -- redis-cli CONFIG SET client-output-buffer-limit 'normal 0 0 0 slave 268435456 67108864 60 pubsub 33554432 8388608 60')"
  [[ "$result" == "OK" ]]
  kubectl -n "$NAMESPACE" exec "deployment/$service" -- redis-cli CONFIG GET client-output-buffer-limit
done
