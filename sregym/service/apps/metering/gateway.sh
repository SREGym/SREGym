#!/bin/bash
# API gateway meter flush: once a second, send the buffered meter records to core.
set -u
mkdir -p /run/load
[ -f /run/load/rate ] || echo "${GATEWAY_INITIAL_RATE:-40}" > /run/load/rate
while true; do
  request=$(cat /run/load/rate 2>/dev/null || echo 0)
  rate=${request%% *}
  if [ "${rate:-0}" -gt 0 ]; then
    psql -XAtq -v ON_ERROR_STOP=1 -v n="$rate" >/dev/null 2>&1 <<'SQL'
insert into meter_inbox (record_id, tenant_id, metric, quantity, occurred_at)
select gen_random_uuid()::text, t, m,
       case m when 'api_calls' then 1 + floor(random() * 5000)
              when 'tokens_k' then round((1 + random() * 800)::numeric, 3)
              else round((0.5 + random() * 40)::numeric, 3) end,
       clock_timestamp() - random() * interval '20 seconds'
from (select (string_to_array('@TENANTS@', ','))[1 + floor(random() * array_length(string_to_array('@TENANTS@', ','), 1))::int] as t,
             (array['api_calls', 'tokens_k', 'storage_gb_h'])[1 + floor(random() * 3)::int] as m
      from generate_series(1, :n)) s;
SQL
  else
    echo "$request" > /run/load/observed-rate
  fi
  sleep 1
done
