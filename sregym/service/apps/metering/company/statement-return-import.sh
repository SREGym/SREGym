#!/bin/bash
# Process the queue in delivery order and acknowledge the completed import in support.
set -euo pipefail
pending=$(mktemp)
trap 'rm -f "$pending"' EXIT
psql -Xq -v ON_ERROR_STOP=1 --dbname=support \
    -c '\copy (select delivery_id, account, return_ref, period_start, period_end, accepted_rows, statement_digest from statement_return_inbox where imported_at is null order by delivery_id) to stdout' \
    > "$pending"
[ -s "$pending" ] || exit 0

psql -Xq -1 -v ON_ERROR_STOP=1 --dbname=finance \
    -c 'create temp table delivery (delivery_id bigint, account text, return_ref text, period_start timestamptz,
                                    period_end timestamptz, accepted_rows jsonb, statement_digest text)' \
    -c "\\copy delivery from '$pending'" \
    -c '
        do $import$
        declare
            item delivery;
        begin
            for item in select * from delivery order by delivery_id loop
                perform reconcile_statement_return(item.account, item.return_ref, item.statement_digest,
                                                   item.period_start, item.period_end, item.accepted_rows);
            end loop;
        end $import$;
    '

# Finance commits first. The queue remains replayable if support is interrupted.
ids=$(cut -f1 "$pending" | paste -sd, -)
acknowledge=true
if [[ ${1:-} == --no-ack ]]; then
    acknowledge=false
fi
psql -Xq -1 -v ON_ERROR_STOP=1 --dbname=support -v delivery_ids="$ids" -v acknowledge="$acknowledge" -f - <<'SQL'
update statement_return_inbox set imported_at = now()
where imported_at is null and delivery_id = any (string_to_array(:'delivery_ids', ',')::bigint[]);
\if :acknowledge
select acknowledge_statement_return(account, return_ref)
from (select distinct account, return_ref from statement_return_inbox
      where delivery_id = any (string_to_array(:'delivery_ids', ',')::bigint[])) completed;
\endif
SQL
