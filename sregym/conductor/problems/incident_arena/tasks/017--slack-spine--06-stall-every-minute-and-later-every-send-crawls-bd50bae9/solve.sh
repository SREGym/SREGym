#!/usr/bin/env bash
# Golden trajectory for the OracleAgent: the minimal, correct, targeted COMPOUND fix.
#
# Runs in the `main` operator-shell pod. Two independent faults on two services:
#   LEG B (channel) — at t=40 s of the load epoch the generator activated the
#   runtime `read_consistency_strict` event on svc-channel. From that instant the
#   send path's /authz/resolve stops hitting the in-process membership/ACL cache
#   and re-queries Postgres per call under ACL_HOLD_MS, saturating svc-channel;
#   sends slow two hops downstream, peak and trough alike, and stay slow. The
#   event set is NEVER in /admin/config — only GET /admin/event shows it. Repair =
#   ONE PUT setting the same event inactive. No restart: the event lives in the
#   process, so a pod replacement is neither needed nor wanted (it would also void
#   the durable-schedule evidence leg A is graded on).
#   LEG A (db) — the durable maintenance controller runs a real PostgreSQL
#   CHECKPOINT every 60 s at offset 35, inside the recurring write peak: every
#   send in that 8 s window stalls but succeeds. Repair = ONE PUT moving the
#   offset into the trough, preserving enabled / period 60 / duration 8 (06-F4's
#   repair; offset 55 satisfies the late-trough envelope, >= 50 and < 60).
#
# The message db pool is HEALTHY here (20/10, peer-uniform). It is read below only
# to show that the reflex capacity fix has nothing to do; touching it would be
# over-repair against the repair_scope ceilings.
#
# Order: the runtime event first, then the schedule. Nothing here replaces a pod,
# so the order is not load-bearing — it just reads better to clear the emergent
# regression before the standing recurrent one.
#
# TIMING — leg B is TIMED, so the golden trajectory must OBSERVE it before it can
# repair it. The push lands at t=40 s of the load epoch and a PUT issued before
# that deactivates an event that is not active yet: a no-op the generator then
# undoes at t=40, inside the graded soak (the soak begins at max(declare_ts,
# warmup 30 s), runner.py:523-529). In practice this profile's `maintenance_epoch`
# event is `required` with `require_manifestation` + `release_agent_on_recovery`,
# so the oracle is released only after leg A's maintenance has manifested and the
# t=40 push has usually already landed — the bounded poll below is a guard against
# a mis-fire, not the common path.
set -euo pipefail

MAINTENANCE_URL="http://db-maintenance:8081"
MSG_BASE="http://svc-message:8000"
CHAN_BASE="http://svc-channel:8000"
EVENT_WAIT_S=120

echo "[solve] current durable maintenance state (expect offset 35, inside the peak):"
curl -fsS "${MAINTENANCE_URL}/v1/maintenance"
echo
echo "[solve] db pool config (message vs a peer role — both healthy 20/10 here):"
curl -fsS "${MSG_BASE}/admin/config"
echo
curl -fsS "${CHAN_BASE}/admin/config"
echo
echo "[solve] runtime event state on svc-channel (the event set does NOT appear in"
echo "        /admin/config above; read_consistency_strict lands at t=40 s):"
curl -fsS "${CHAN_BASE}/admin/event"
echo

# Wait for the timed push to actually land, bounded so a genuinely missing event
# fails loudly here instead of as an unexplained outcome miss 150 s later.
echo "[solve] waiting up to ${EVENT_WAIT_S}s for read_consistency_strict on svc-channel:"
deadline=$((SECONDS + EVENT_WAIT_S))
while :; do
  events="$(curl -fsS "${CHAN_BASE}/admin/event" 2>/dev/null || true)"
  case "${events}" in *read_consistency_strict*) break ;; esac
  if [ "${SECONDS}" -ge "${deadline}" ]; then
    echo "[solve] FATAL: read_consistency_strict never activated on svc-channel;" >&2
    echo "[solve] last /admin/event body: ${events}" >&2
    exit 1
  fi
  sleep 2
done
echo "[solve] push observed after ~${SECONDS}s: ${events}"

# LEG B: turn the pushed consistency toggle back off on svc-channel. One action.
echo "[solve] deactivating read_consistency_strict on svc-channel:"
curl -fsS -X PUT "${CHAN_BASE}/admin/event" \
  -H 'content-type: application/json' \
  --data '{"name":"read_consistency_strict","active":false}'
echo

# LEG A: preserve real checkpoint maintenance, its period and duration; move only
# the recurring offset from the write peak into the trough.
echo "[solve] moving the maintenance schedule out of the write peak:"
curl -fsS -X PUT "${MAINTENANCE_URL}/v1/maintenance" \
  -H 'content-type: application/json' \
  --data '{"enabled":true,"period_s":60,"offset_s":55,"duration_s":8}'
echo

echo "[solve] confirming both repairs:"
echo "[solve] svc-channel events after (expect read_consistency_strict absent):"
curl -fsS "${CHAN_BASE}/admin/event"
echo
echo "[solve] durable maintenance after (expect enabled, period 60, offset 55, duration 8):"
curl -fsS "${MAINTENANCE_URL}/v1/maintenance"
echo

# ONE report naming BOTH (service, component) pairs.
submit_incident_report --help >/dev/null
submit_incident_report <<'JSON'
{"findings": [
  {
    "service": "channel",
    "component": "channel.membership-acl",
    "mechanism": "A runtime consistency toggle pushed onto the channel service partway through the episode made its channel-to-org membership/ACL resolve bypass the in-process cache and re-query the database on every send, holding a channel-service connection per resolve. Because the send path resolves authorization before persisting, every send slowed from that instant on, in peak and trough alike. I turned the same runtime toggle back off on the channel service, which restored the cached resolve immediately without a restart or a configuration edit."
  },
  {
    "service": "db",
    "component": "db.maintenance-controller",
    "mechanism": "Independently, the durable database maintenance controller scheduled real PostgreSQL checkpoints every 60 seconds at offset 35 from the load epoch, so each 8-second run overlapped the recurring write peak and every send in that window stalled. I preserved enabled checkpoint maintenance with its period and duration and moved the complete schedule to offset 55 so future checkpoints execute in the trough."
  }
]}
JSON

echo "[solve] incident report filed; runtime toggle reverted, schedule moved to the trough."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
