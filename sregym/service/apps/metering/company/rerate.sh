#!/bin/bash
# Re-rates every ledger line from its usage event and the current price book.
set -euo pipefail
n=$(psql -XAtq -v ON_ERROR_STOP=1 <<'SQL'
with rerated as (
  update ledger_lines l
  set account = u.tenant_id, metric = u.metric, quantity = u.quantity, amount = round(u.quantity * p.unit_price, 4)
  from usage_events u join price_book p using (metric)
  where u.event_id = l.line_id
  returning 1)
select count(*) from rerated;
SQL
)
echo "$(date -u +%FT%TZ) re-rated $n ledger lines"
