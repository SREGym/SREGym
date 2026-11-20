#!/bin/bash
# Sends each listed account a file of every usage correction that touched it
# (before or after) since that account's last file.
set -euo pipefail
conf=${1:-/etc/extracts/extracts.conf}

q() { psql -XAtq -v ON_ERROR_STOP=1 "$@"; }
log() { echo "$(date -u +%FT%TZ) $*"; }

while IFS='|' read -r account ticket; do
  account=$(sed 's/^ *//; s/ *$//' <<<"$account")
  ticket=$(sed 's/^ *//; s/ *$//' <<<"$ticket")
  [[ -z "$account" || "$account" == \#* ]] && continue
  last=$(q -d support -v account="$account" <<'SQL'
select coalesce(max((r->>'correction_id')::bigint), 0)
from extract_files f cross join jsonb_array_elements(f.rows) r where f.account = :'account';
SQL
)
  rows=$(q -d usage -v account="$account" -v last="$last" <<'SQL'
select coalesce(jsonb_agg(jsonb_build_object('correction_id', correction_id, 'event_id', event_id, 'kind', kind,
                                             'before', before, 'after', after) order by correction_id), '[]')
from usage_corrections
where correction_id > :last and (before->>'tenant_id' = :'account' or after->>'tenant_id' = :'account');
SQL
)
  if [ "$rows" = "[]" ]; then
    log "$account: no corrections since the last file"
    continue
  fi
  file=$(q -d support -v account="$account" -v ticket="$ticket" -v rows="$rows" <<'SQL'
insert into extract_files (ticket_id, account, status, rows)
values (nullif(:'ticket', ''), :'account', 'ready', :'rows'::jsonb) returning file_id;
SQL
)
  log "$account: sent file $file with $(q -d support -v file="$file" <<<"select jsonb_array_length(rows) from extract_files where file_id = :file;") corrections"
done <"$conf"
