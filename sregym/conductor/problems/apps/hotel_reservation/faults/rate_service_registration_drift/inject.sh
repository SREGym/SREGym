#!/usr/bin/env bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-hotel-reservation}"
export ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
for COMPONENT in user rate mongodb-user; do
  kubectl -n "$NAMESPACE" wait --for=condition=Ready pod -l "io.kompose.service=$COMPONENT" --timeout=120s
done
CURRENT_OWNER=$(kubectl -n "$NAMESPACE" get deployment consul -o jsonpath='{.metadata.uid}')
SAVED_OWNER=""
if [[ -f "$ARTIFACT_DIR/directory-original-owner" ]]; then
  read -r SAVED_OWNER < "$ARTIFACT_DIR/directory-original-owner" || true
fi
if [[ ! -s "$ARTIFACT_DIR/directory-original.json" || "$SAVED_OWNER" != "$CURRENT_OWNER" ]]; then
  python3 "$ARTIFACT_DIR/directory_tools.py" snapshot > "$ARTIFACT_DIR/directory-original.json.tmp"
  mv "$ARTIFACT_DIR/directory-original.json.tmp" "$ARTIFACT_DIR/directory-original.json"
  printf '%s\n' "$CURRENT_OWNER" > "$ARTIFACT_DIR/directory-original-owner"
fi
python3 - "$ARTIFACT_DIR/directory_refresh.js" <<'PY' | kubectl -n "$NAMESPACE" exec -i deploy/mongodb-user -- bash -c 'cat > /tmp/directory_refresh.js'
import json
import os
import subprocess
import sys
inventory = json.loads(subprocess.check_output(['kubectl', '-n', os.environ['NAMESPACE'], 'exec', 'deploy/user', '--', 'cat', 'config.json']))
print('var inventory = ' + json.dumps(inventory) + ';')
print(open(sys.argv[1]).read())
PY
kubectl -n "$NAMESPACE" exec deploy/mongodb-user -- bash -c 'DIRECTORY_SINGLE_PASS=1 mongo --nodb --quiet /tmp/directory_refresh.js'
kubectl -n "$NAMESPACE" exec -i deploy/mongodb-user -- bash -s <<'SH_REMOTE'
set -euo pipefail
RUNNING=0
for COMMAND_FILE in /proc/[0-9]*/cmdline; do
  while IFS= read -r -d '' ARGUMENT; do
    if [[ "$ARGUMENT" == /tmp/directory_refresh.js ]]; then RUNNING=1; fi
  done < "$COMMAND_FILE" 2>/dev/null || true
done
if [[ "$RUNNING" == 0 ]]; then
  nohup mongo --nodb --quiet /tmp/directory_refresh.js > /tmp/directory_refresh.log 2>&1 < /dev/null &
  echo "$!" > /tmp/directory_refresh.pid
fi
printf '%s\n' 'Service directory refresh is running'
SH_REMOTE
