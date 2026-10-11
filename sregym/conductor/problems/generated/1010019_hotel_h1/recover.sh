#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}/common.sh"
{ printf 'var rows='; cat "$STATE_DIR/hotels.ejson"; printf '; db.hotels.remove({}); rows.forEach(function(r){db.hotels.insert(r);});\n'; } | kubectl -n "$NAMESPACE" exec -i deploy/mongodb-profile -- mongo profile-db --quiet
clear_cache
