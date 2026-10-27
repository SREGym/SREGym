#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-social-network}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
export NAMESPACE ARTIFACT_DIR
bash "$ARTIFACT_DIR/mitigate.sh"

# Restore the exact pre-incident native limits when the captured deployment
# still owns these Services. No application records or manifests are changed.
python3 - <<'PY'
import json, os, pathlib, subprocess
ns = os.environ['NAMESPACE']
path = pathlib.Path(os.environ['ARTIFACT_DIR']) / 'original.json'
if not path.exists():
    raise SystemExit(0)
state = json.loads(path.read_text())
for service, limit in state['limits'].items():
    base = ['kubectl','-n',ns]
    uid = json.loads(subprocess.check_output(base+['get','service',service,'-o','json'],text=True))['metadata']['uid']
    if uid != state['uids'][service]:
        continue
    result = subprocess.check_output(base+['exec','deployment/'+service,'--','redis-cli','CONFIG','SET','client-output-buffer-limit',limit],text=True).strip()
    if result != 'OK':
        raise RuntimeError(result)
PY
