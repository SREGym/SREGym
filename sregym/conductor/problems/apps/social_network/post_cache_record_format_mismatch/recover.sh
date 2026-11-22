#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}
export NAMESPACE
bash "$ARTIFACT_DIR/mitigate.sh"
if [[ -f "$ARTIFACT_DIR/original-deployments.json" ]]; then
  python3 - "$ARTIFACT_DIR/original-deployments.json" "$NAMESPACE" <<'PY'
import json, subprocess, sys
with open(sys.argv[1]) as f:
    original = json.load(f)
names = []
for deployment in original["items"]:
    name = deployment["metadata"]["name"]
    names.append(name)
    template = deployment["spec"]["template"]
    patch = [{"op": "replace", "path": "/spec/template", "value": template}]
    subprocess.run(["kubectl", "-n", sys.argv[2], "patch", "deployment", name,
                    "--type=json", "-p", json.dumps(patch)], check=True)
for name in names:
    subprocess.run(["kubectl", "-n", sys.argv[2], "rollout", "status",
                    "deployment/" + name, "--timeout=180s"], check=True)
PY
fi
