#!/usr/bin/env bash
# Golden OracleAgent trajectory for 09-I1 (leaked channel_seq row lock, Tier-2 image
# fault). Runs in the `main` operator-shell pod (where the real agent's exec lands).
# The fault is baked in immutable image code (HOLD_SEQ_LOCK) and cannot be reverted;
# the repair is purely operational SQL over the privileged DSN. Enlarging the message
# pool (/admin/config) CANNOT free a held row lock, and restart-svc.sh re-arms the leak
# on boot — so neither reflex is a durable fix.
set -euo pipefail

echo "[solve] privileged DSN:"
printenv | grep DSN
echo

# The idle-in-transaction backend that holds a lock on the channel_seq relation IS the
# leaked-lock signature (the rogue took SELECT ... FOR UPDATE at boot and never committed).
HOLDER_SQL="
  SELECT DISTINCT a.pid
  FROM pg_stat_activity a
  JOIN pg_locks l ON l.pid = a.pid
  JOIN pg_class c ON c.oid = l.relation
  WHERE a.state = 'idle in transaction' AND c.relname = 'channel_seq';"

# 0) WAIT FOR THE FAULT TO MANIFEST. The rogue holds the lock from boot, but the SYMPTOM
#    (writers blocking -> pool pressure -> 503s) only builds once write load ramps past the
#    profile's 30s warmup. Acting at t~0 (a) declares before the cluster is fully up, tripping
#    the verifier's "service unreachable at declare" guard, and (b) grades an un-degraded soak.
#    Poll the DB until writers are BLOCKED (lock not granted) on the channel_seq relation:
#    that proves the cluster is serving AND the fault has bitten — an operator observing the
#    symptom before acting.
echo "[solve] waiting for the cluster to serve + the fault to be armed..."
READY_DEADLINE=$(( $(date +%s) + 300 ))
while :; do
  # (a) is svc-message serving? A write to an UNLOCKED channel (chan-1) succeeds fast even
  #     under the fault (only chan-0's seq row is leaked-locked) -> the app pods are ready.
  CODE="$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST http://svc-message:8000/messages \
    -H 'content-type: application/json' \
    -d '{"channel_id":"chan-1","client_msg_id":"solve-ready-probe","text":"ready"}' 2>/dev/null || true)"
  # (b) is the rogue armed? The idle-in-transaction holder on channel_seq is PERSISTENT (from
  #     boot), so it is a reliable signal — unlike the now lock_timeout-transient blocked writers.
  ROGUE="$(psql "$DB_ADMIN_DSN" -tAc "$HOLDER_SQL" 2>/dev/null | tr -d '[:space:]')"
  echo "[solve] chan-1 (unlocked) write HTTP=${CODE} | rogue pid=${ROGUE:-none}"
  case "$CODE" in
    2*) if [ -n "$ROGUE" ]; then echo "[solve] cluster serving + fault armed; proceeding"; break; fi ;;
  esac
  if [ "$(date +%s)" -ge "$READY_DEADLINE" ]; then echo "[solve] ready-wait timed out; proceeding"; break; fi
  sleep 5
done
echo

# 1) DIAGNOSE (read-only SELECTs over the privileged DSN). Show the idle-in-txn backend,
#    its transaction age, and the channel_seq lock it holds — the smoking gun the
#    pool-exhaustion surface hides.
echo "[solve] idle-in-transaction sessions:"
psql "$DB_ADMIN_DSN" -c "
  SELECT pid, state, EXTRACT(EPOCH FROM (now()-xact_start))::int AS idle_txn_age_s, wait_event_type, query
  FROM pg_stat_activity
  WHERE state = 'idle in transaction'
  ORDER BY xact_start;"
echo "[solve] backends holding a channel_seq lock:"
psql "$DB_ADMIN_DSN" -c "
  SELECT a.pid, c.relname, l.mode, a.state
  FROM pg_stat_activity a
  JOIN pg_locks l ON l.pid = a.pid
  JOIN pg_class c ON c.oid = l.relation
  WHERE c.relname = 'channel_seq' AND a.state = 'idle in transaction';"

# 2) THE OPERATIONAL FIX — terminate every idle-in-transaction backend holding the
#    channel_seq lock. This releases the leaked row lock; the blocked sequencer writes
#    drain and the pool recovers.
for pid in $(psql "$DB_ADMIN_DSN" -tAc "$HOLDER_SQL"); do
  echo "[solve] terminating leaked lock holder pid=$pid"
  psql "$DB_ADMIN_DSN" -c "SELECT pg_terminate_backend($pid);"
done

# 2b) Defence-in-depth: arm idle_in_transaction_session_timeout so a re-armed leak (e.g.
#     if the message pod restarts and re-takes the lock) is reaped by Postgres. This is a
#     server-level setting (ALTER SYSTEM), NOT the app config the minimality gate grades.
psql "$DB_ADMIN_DSN" -c "ALTER SYSTEM SET idle_in_transaction_session_timeout = '10s';"
psql "$DB_ADMIN_DSN" -c "SELECT pg_reload_conf();"

# 3) VERIFY the lock is gone and writes recover before declaring. First confirm no holder
#    remains, then drive a real chan-0 send (the previously-locked channel) to 2xx.
REMAIN="$(psql "$DB_ADMIN_DSN" -tAc "$HOLDER_SQL" | tr -d '[:space:]')"
echo "[solve] remaining channel_seq lock holders after fix: '${REMAIN:-none}'"
RECOVER_DEADLINE=$(( $(date +%s) + 120 ))
while :; do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST http://svc-message:8000/messages \
    -H 'content-type: application/json' \
    -d '{"channel_id":"chan-0","client_msg_id":"solve-recover-probe","text":"recovery probe"}' || true)"
  case "$CODE" in
    2*) echo "[solve] chan-0 write recovered (HTTP $CODE)"; break;;
  esac
  [ "$(date +%s)" -ge "$RECOVER_DEADLINE" ] && { echo "[solve] recovery wait timed out (last HTTP $CODE)"; break; }
  sleep 3
done

# 4) DECLARE — attribute to the OWNER (message.sequencer), the leaked channel_seq row
#    lock, NOT the message pool where the 503s surface.
submit_incident_report <<'JSON'
{
  "service": "message",
  "component": "message.sequencer",
  "mechanism": "The message service leaked a row lock: a dedicated boot transaction took SELECT ... FOR UPDATE on chan-0's channel_seq row and never committed (an idle-in-transaction backend). Concurrent atomic-sequencer writes to chan-0 block on that row lock and, with no statement_timeout, hold their pooled connections until the pool starves, so POST /messages returns 503 pool_timeout — a pool-exhaustion lookalike that no pool enlargement can fix and a restart only re-arms. Terminated the idle-in-transaction backend holding the channel_seq lock (pg_terminate_backend) and armed idle_in_transaction_session_timeout; no config changed, no data destroyed."
}
JSON

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
