#!/bin/bash
set -euo pipefail
while true; do
 if ! mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db /catalog/reconcile.js; then echo 'Catalog publication unavailable; retrying'; fi
 sleep 30
done
