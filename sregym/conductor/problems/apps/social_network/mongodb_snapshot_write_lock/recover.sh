#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}
export NAMESPACE
# No manifests, documents, indexes, credentials or cache entries are changed.
# Removing the detached coordinator and releasing its write freeze restores
# the original runtime state. The baseline assertion prevents overwriting an
# administrative freeze which predated this operation.
if [[ -f "$ARTIFACT_DIR/state/original.json" ]]; then
  python3 -c 'import json,sys; assert not json.load(open(sys.argv[1]))["locked"]' "$ARTIFACT_DIR/state/original.json"
fi
bash "$ARTIFACT_DIR/mitigate.sh"
