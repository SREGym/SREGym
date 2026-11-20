#!/bin/bash
# Apply each delivery to finance before marking it imported in the support inbox.
# Delivery acknowledgement is recorded separately in support.
set -euo pipefail
deliveries=$(psql -XAtq -v ON_ERROR_STOP=1 --dbname=support <<'SQL'
select delivery_id from statement_return_inbox where imported_at is null order by delivery_id;
SQL
)
while IFS= read -r id; do
    [ -n "$id" ] || continue
    waived=$(
        psql -Xq -v ON_ERROR_STOP=1 --dbname=support \
            -c "\copy (select account, return_ref, period_start, period_end, accepted_rows from statement_return_inbox where delivery_id = $id) to stdout" |
        psql -XAtq -v ON_ERROR_STOP=1 --dbname=finance \
            -c 'create temp table delivery (account text, return_ref text, period_start timestamptz,
                                            period_end timestamptz, accepted_rows jsonb)' \
            -c '\copy delivery from pstdin' \
            -c 'select import_statement_return(account, return_ref, period_start, period_end, accepted_rows)
                from delivery'
    )
    psql -Xq -v ON_ERROR_STOP=1 --dbname=support -v delivery_id="$id" <<'SQL'
update statement_return_inbox set imported_at = now() where delivery_id = :delivery_id and imported_at is null;
SQL
    echo "Imported delivery $id ($waived new waivers)"
done <<< "$deliveries"
