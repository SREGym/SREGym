#!/usr/bin/env bash
# Restart procnode in place. Like a node reboot: it reaps the old sandboxes
# and re-runs every pod bound to its nodes, keeping each pod's IP.
#
# usage: restart-procnode.sh [cluster-name]
set -euo pipefail

NAME=${1:-mini}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
STATE=${STATE:-/var/lib/procnode-cluster/$NAME}
PYTHON=${PYTHON:-python3}
NODE_IP=${NODE_IP:-$(ip -4 route get 1.1.1.1 | awk '{for (i=1;i<NF;i++) if ($i=="src") print $(i+1)}')}
POD_CIDR=${POD_CIDR:-10.88.0.0/16}
KCFG=$HOME/.kwok/clusters/$NAME/kubeconfig.yaml

pkill -f "procnode/procnode[.]py --kubeconfig $KCFG" || true
sleep 2
setsid nohup "$PYTHON" "$HERE/../procnode/procnode.py" --kubeconfig "$KCFG" --node-ip "$NODE_IP" \
  --primary-node kind-worker --pod-cidr "$POD_CIDR" --dns-ip 10.96.0.10 \
  > "$STATE/procnode.log" 2>&1 < /dev/null &
echo "procnode restarted"
