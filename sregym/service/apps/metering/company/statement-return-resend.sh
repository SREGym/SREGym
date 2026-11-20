#!/bin/bash
set -euo pipefail
for database in pell_usage quill_usage larkspur_live ostrava_usage; do
    psql -XAtq -v ON_ERROR_STOP=1 --dbname="$database" -c 'select send_allocation_returns();'
done
