#!/bin/bash
set -euo pipefail
export NAMESPACE=${NAMESPACE:-hotel-reservation}
DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
python3 "$DIR/control.py" inject
