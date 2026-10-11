#!/bin/bash
set -euo pipefail
export NAMESPACE=${NAMESPACE:-hotel-reservation}
DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python3 "$DIR/control.py" mitigate
