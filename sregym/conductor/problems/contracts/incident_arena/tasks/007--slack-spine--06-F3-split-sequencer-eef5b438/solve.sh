#!/usr/bin/env bash
# Golden trajectory for the OracleAgent: the minimal, correct, targeted DUAL fix.
#
# Runs in the `main` operator-shell pod (exactly where a real agent's exec lands).
# This is a SILENT per-channel ordering corruption (no 5xx): the message service's
# per-channel sequencer is running its NON-ATOMIC read-modify-write path
# (SEQUENCER_MODE=rmw) instead of the shipped atomic
# INSERT ... ON CONFLICT DO UPDATE ... RETURNING. Under concurrent same-channel sends
# two requests both read the same last_seq and write the SAME next seq (a lost update),
# so two distinct messages persist with the SAME per-channel `seq`. Every send still
# returns 200/201 and search still works — the only observable is the persisted
# ordering (GET /channels/<cid>/messages shows duplicate seq).
#
# The fix is DUAL and the ORDER matters:
#   1) STOP the ongoing cause: persist "atomic" through the one-way fixed-target
#      configuration helper. The broker changes only svc-message's pod-template env and
#      waits for its replacement to become ready. This must come FIRST so reconciliation in
#      step 2 is not immediately re-corrupted by freshly-minted duplicates.
#   2) RECONCILE the already-corrupted rows with the privileged psql DSN: re-sequence the
#      duplicates per channel so each channel's seq run is contiguous + unique again,
#      WITHOUT dropping any message row (NOT a destructive TRUNCATE/DELETE — every message
#      must survive). Then realign channel_seq.last_seq to each channel's new max.
#
# The agent has a closed `set-sequencer-mode` helper and `psql` (no kubectl) in the
# operator shell. The helper accepts only atomic and targets only svc-message.
set -euo pipefail

MSG_BASE="http://svc-message:8000"

echo "[solve] privileged DSN:"
printenv | grep DSN
echo

# 1) Diagnose: the running sequencer mode is the non-atomic read-modify-write path, and
#    reading channel history back shows duplicate per-channel seq under concurrency.
echo "[solve] sequencer mode before (expect the non-atomic mode):"
curl -fsS "${MSG_BASE}/admin/sequencer"
echo
echo "[solve] channels carrying DUPLICATE per-channel seq (the corruption):"
psql "$DB_ADMIN_DSN" -c \
  "SELECT channel_id, seq, count(*) AS n FROM messages GROUP BY channel_id, seq HAVING count(*) > 1 ORDER BY channel_id, seq;"
echo

# 2a) STOP THE CAUSE (first): persist atomic mode in svc-message's pod template. The
#     one-way helper waits for the resulting service replacement to become ready before
#     returning, so subsequent writes cannot use the injected RMW mode.
echo "[solve] durably selecting atomic mode on svc-message:"
broker_output=""
broker_rc=0
broker_output="$(set-sequencer-mode atomic 2>&1)" || broker_rc=$?
printf '%s\n' "$broker_output"
if [ "$broker_rc" -ne 0 ]; then
  echo "[solve] broker response was non-success; retrying once and preserving the broker error body" >&2
  retry_body=""
  retry_status=""
  retry_body="$(mktemp)"
  retry_status="$(curl --noproxy sequencer-config-broker -sS \
    -o "$retry_body" -w '%{http_code}' \
    -X POST -H 'Content-Type: application/json' \
    --data '{"mode":"atomic"}' \
    "${SEQUENCER_CONFIG_BROKER_URL%/}/v1/sequencer-mode")" || retry_status="curl_failed"
  cat "$retry_body"
  rm -f "$retry_body"
  echo
  echo "[solve] broker retry HTTP status: $retry_status"
fi

# The broker persists the pod-template mode before observing the replacement. A
# late/transient error while it watches that rollout must not make its HTTP response
# the source of truth. Conversely, do not continue merely because the broker was
# called: only a replacement process started from the persisted atomic mode can make
# this endpoint report atomic (the repair path never uses the live PUT knob).
echo "[solve] replacement sequencer mode after (expect atomic):"
mode_after=""
for _attempt in $(seq 1 30); do
  mode_after="$(curl -fsS "${MSG_BASE}/admin/sequencer" 2>/dev/null || true)"
  if printf '%s\n' "$mode_after" | grep -Eq '"mode"[[:space:]]*:[[:space:]]*"atomic"'; then
    break
  fi
  sleep 1
done
printf '%s\n' "$mode_after"
if ! printf '%s\n' "$mode_after" | grep -Eq '"mode"[[:space:]]*:[[:space:]]*"atomic"'; then
  echo "[solve] FATAL: svc-message did not restart from the persisted atomic mode (broker rc=$broker_rc)" >&2
  exit 1
fi
echo

# 2b) RECONCILE THE DATA (second, after the replacement is ready) — RACE-FREE under load.
#     The reconciliation runs in ONE transaction that FIRST locks every per-channel cursor
#     row (SELECT ... FOR UPDATE on channel_seq): in-flight sends holding a cursor lock drain,
#     and new sends BLOCK at their channel_seq upsert until this transaction commits — so no
#     send can interleave between the renumber and the cursor realign and leave a one-off
#     boundary gap (a naive two-statement reconcile races the live write load and can leave a
#     single gap that the zero-tolerance seq_integrity gate fails on). Inside the lock:
#     re-sequence each channel's messages to a dense 1..count run by insertion order
#     (seq = ROW_NUMBER() OVER (PARTITION BY channel_id ORDER BY id) — message ORDER preserved,
#     every row kept; this is an UPDATE, never a DELETE/TRUNCATE, so no data is destroyed),
#     then realign channel_seq.last_seq to each channel's new max so blocked/subsequent atomic
#     sends continue the contiguous run with no gap and no re-collision.
echo "[solve] re-sequencing duplicate per-channel seq under a cursor lock (race-free, keeping every row):"
psql "$DB_ADMIN_DSN" -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
-- Serialize against live sends: lock every per-channel cursor row so in-flight upserts
-- drain and new sends block at their channel_seq upsert until COMMIT.
SELECT 1 FROM channel_seq WHERE channel_id LIKE 'chan-%' FOR UPDATE;
WITH renum AS (
  SELECT id,
         ROW_NUMBER() OVER (PARTITION BY channel_id ORDER BY id) AS new_seq
    FROM messages
)
UPDATE messages m
   SET seq = r.new_seq
  FROM renum r
 WHERE m.id = r.id
   AND m.seq <> r.new_seq;
UPDATE channel_seq cs
   SET last_seq = mx.max_seq
  FROM (SELECT channel_id, max(seq) AS max_seq FROM messages GROUP BY channel_id) mx
 WHERE cs.channel_id = mx.channel_id
   AND cs.last_seq <> mx.max_seq;
COMMIT;
SQL
echo

# 3) Verify: no channel carries a duplicate per-channel seq anymore (expect zero rows).
echo "[solve] duplicate per-channel seq after reconciliation (expect none):"
psql "$DB_ADMIN_DSN" -c \
  "SELECT channel_id, seq, count(*) AS n FROM messages GROUP BY channel_id, seq HAVING count(*) > 1 ORDER BY channel_id, seq;"
echo

# 4) File the incident report (advisory; declare_repair_complete below ends the episode). Attribute to the
#    send-path component that OWNS the cause (message / message.sequencer) — NOT search or
#    the db where the duplicate-seq symptom merely surfaces.
submit_incident_report --help >/dev/null
submit_incident_report <<'JSON'
{
  "service": "message",
  "component": "message.sequencer",
  "mechanism": "The message service's per-channel sequencer was running its non-atomic read-modify-write path (a plain un-locked SELECT of last_seq, then a separate upsert writing the read-computed next) instead of the atomic INSERT ... ON CONFLICT DO UPDATE ... RETURNING, so under concurrent same-channel sends two requests both read the same last_seq and assigned the SAME next seq (a lost update) — two distinct messages persisted with the same per-channel seq, while every send still returned success. Persisting atomic mode through the fixed-target helper replaced svc-message and stopped new duplicates, and re-sequencing the already-corrupted rows per channel (ROW_NUMBER by insertion order, every row kept) restored a contiguous, unique seq run without destroying data."
}
JSON

echo "[solve] incident report filed; atomic replacement ready, duplicate seq reconciled, channel cursors realigned."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
