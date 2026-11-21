#!/bin/bash
# Moves gateway meter records from the meter inbox into usage_events.
#
# Each worker owns one shard of the inbox. A batch stays open for one flush
# window: it is opened with its header, takes whatever records arrive during
# the window, and commits them together.
set -u
workers=${INGEST_WORKERS:-2}
replica=${HOSTNAME##*-}  # StatefulSet ordinal
shards=${INGEST_SHARDS:-4}
window_steps=${INGEST_WINDOW_STEPS:-5}
step_seconds=${INGEST_STEP_SECONDS:-0.2}

take() {
  cat <<SQL
with taken as (
  delete from meter_inbox where id % $shards = :shard
  returning record_id, tenant_id, metric, quantity, occurred_at)
insert into usage_events (record_id, tenant_id, metric, quantity, occurred_at, batch_id)
select record_id, tenant_id, metric, quantity, occurred_at, currval('ingest_batches_batch_id_seq')
from taken order by occurred_at
on conflict (record_id) do nothing;
SQL
}

batch() {
  echo "begin;"
  echo "insert into ingest_batches (source) values (:'source');"
  for _ in $(seq 1 "$window_steps"); do
    take
    echo "\\! sleep $step_seconds"
  done
  echo "update ingest_batches set records = (select count(*) from usage_events where batch_id = currval('ingest_batches_batch_id_seq')) where batch_id = currval('ingest_batches_batch_id_seq');"
  echo "commit;"
}

work() {
  local shard=$1 source="$(hostname)/$1"
  while true; do
    if [ "$(psql -XAtq -v shard="$shard" <<<"select exists (select 1 from meter_inbox where id % $shards = :shard);" 2>/dev/null)" != t ]; then
      sleep 1
      continue
    fi
    batch | psql -XAtq -v ON_ERROR_STOP=1 -v source="$source" -v shard="$shard" >/dev/null || sleep 2
  done
}

# Replica N runs workers for shards N*workers .. N*workers+workers-1.
for i in $(seq 0 $((workers - 1))); do
  work $(( (replica * workers + i) % shards )) &
done
wait
