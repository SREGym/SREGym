#!/bin/bash
# Process the queue in delivery order and acknowledge the completed import in support.
set -euo pipefail
pending=$(mktemp)
trap 'rm -f "$pending"' EXIT
psql -Xq -v ON_ERROR_STOP=1 --dbname=support \
    -c '\copy (select delivery_id, account, return_ref, period_start, period_end, accepted_rows from statement_return_inbox where imported_at is null order by delivery_id) to stdout' \
    > "$pending"
[ -s "$pending" ] || exit 0

psql -Xq -1 -v ON_ERROR_STOP=1 --dbname=finance \
    -c 'create temp table delivery (delivery_id bigint, account text, return_ref text, period_start timestamptz,
                                    period_end timestamptz, accepted_rows jsonb)' \
    -c "\\copy delivery from '$pending'" \
    -c '
        do $import$
        declare
            item delivery;
            waived integer;
        begin
            for item in select * from delivery order by delivery_id loop
                waived := import_statement_return(item.account, item.return_ref, item.period_start,
                                                  item.period_end, item.accepted_rows);
                raise notice '"'"'Imported delivery % (% new waivers)'"'"', item.delivery_id, waived;
            end loop;
        end $import$;
    '

# If this step is interrupted the same deliveries may arrive again. The merge
# and the per-charge revocations make replay safe.
ids=$(cut -f1 "$pending" | paste -sd, -)
psql -Xq -v ON_ERROR_STOP=1 --dbname=support -v delivery_ids="$ids" <<'SQL'
update statement_return_inbox set imported_at = now()
where imported_at is null and delivery_id = any (string_to_array(:'delivery_ids', ',')::bigint[]);
SQL
