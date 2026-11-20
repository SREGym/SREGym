#!/bin/bash
# Re-rates every ledger line from its usage event and the current price book.
set -euo pipefail
n=$(psql -XAtq -v ON_ERROR_STOP=1 -f /opt/billing/rerate.sql)
echo "$(date -u +%FT%TZ) re-rated $n ledger lines"
