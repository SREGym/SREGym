#!/bin/bash
# Reconciles the database's publications with the declared configuration.
#
# Operations a publication gains are applied first. Before any operation is
# withdrawn, every active subscriber must have confirmed a position past the
# additions, so each change stays published by at least one publication.
set -euo pipefail
conf=${1:-/etc/pubsync/publications.conf}
revision=$(sed -n 's/^# revision: *//p' "$conf" | head -1)
ORDER="insert update delete truncate"

q() { psql -XAtq -v ON_ERROR_STOP=1 "$@"; }
log() { echo "$(date -u +%FT%TZ) $*"; }
trim() { sed 's/^ *//; s/ *$//' <<<"$1"; }

normalize() {
  local out=() op
  for op in $ORDER; do
    [[ ",$(tr -d ' ' <<<"$1")," == *",$op,"* ]] && out+=("$op")
  done
  local IFS=,
  echo "${out[*]}"
}

union() { normalize "$1,$2"; }

current() {
  q -v name="$1" <<'SQL'
select concat_ws(',', case when pubinsert then 'insert' end, case when pubupdate then 'update' end,
                 case when pubdelete then 'delete' end, case when pubtruncate then 'truncate' end)
from pg_publication where pubname = :'name';
SQL
}

# Changes of one phase are applied together, in one transaction, with every
# affected table locked against writers first.
pending=""
locked=""
stage() {
  local name=$1 table=$2 publish=$3 change=$4
  [[ " $locked " == *" $table "* ]] || { pending+="lock table \"$table\" in share row exclusive mode;"$'\n'; locked+=" $table"; }
  pending+="alter publication \"$name\" set (publish = '${publish//,/, }');"$'\n'
  pending+="insert into ops.pubsync_log (revision, publication, change) values ('$revision', '$name', '$change');"$'\n'
  log "$name: $change"
}

apply_staged() {
  [ -z "$pending" ] && return 0
  q <<<"begin;"$'\n'"$pending""commit;"
  pending=""
  locked=""
}

create() {
  local name=$1 table=$2 filter=$3 publish=$4
  q -v name="$name" -v publish="${publish//,/, }" <<<"create publication :\"name\" with (publish = :'publish');"
  local where=""
  [ -n "$filter" ] && where="where ($filter)"
  q -v name="$name" -v table="$table" -v revision="$revision" <<SQL
begin;
lock table :"table" in share row exclusive mode;
alter publication :"name" add table :"table" $where;
insert into ops.pubsync_log (revision, publication, change) values (:'revision', :'name', 'created publish=$publish');
commit;
SQL
  log "$name: created publish=$publish"
}

declare -a names tables filters wants
while IFS='|' read -r name table filter publish; do
  name=$(trim "$name")
  [[ -z "$name" || "$name" == \#* ]] && continue
  names+=("$name"); tables+=("$(trim "$table")"); filters+=("$(trim "$filter")"); wants+=("$(normalize "$publish")")
done <"$conf"

log "revision $revision: ${#names[@]} publications"
added=0
for i in "${!names[@]}"; do
  have=$(current "${names[$i]}")
  if [ -z "$(q -v name="${names[$i]}" <<<"select 1 from pg_publication where pubname = :'name';")" ]; then
    create "${names[$i]}" "${tables[$i]}" "${filters[$i]}" "${wants[$i]}"
    continue
  fi
  more=$(union "$have" "${wants[$i]}")
  if [ "$more" != "$have" ]; then
    stage "${names[$i]}" "${tables[$i]}" "$more" "publish ${have:-none} -> ${more}"
    added=1
  fi
done
apply_staged

if [ "$added" = 1 ]; then
  lsn=$(q -c "select pg_current_wal_lsn()")
  start=$(date +%s)
  while true; do
    behind=$(q -v lsn="$lsn" <<<"select count(*) from pg_replication_slots where slot_type = 'logical' and database = current_database() and active and confirmed_flush_lsn < :'lsn'::pg_lsn;")
    [ "$behind" = 0 ] && break
    if [ $(( $(date +%s) - start )) -gt "${PUBSYNC_CONFIRM_TIMEOUT:-900}" ]; then
      log "$behind subscribers did not confirm $lsn; not withdrawing anything"
      exit 1
    fi
    sleep 2
  done
  slots=$(q -c "select count(*) from pg_replication_slots where slot_type = 'logical' and database = current_database() and active")
  log "waited $(( $(date +%s) - start ))s for $slots subscribers to confirm $lsn"
fi

for i in "${!names[@]}"; do
  have=$(current "${names[$i]}")
  if [ "$have" != "${wants[$i]}" ]; then
    stage "${names[$i]}" "${tables[$i]}" "${wants[$i]}" "publish ${have:-none} -> ${wants[$i]:-none}"
  fi
done
apply_staged
log "revision $revision applied"
