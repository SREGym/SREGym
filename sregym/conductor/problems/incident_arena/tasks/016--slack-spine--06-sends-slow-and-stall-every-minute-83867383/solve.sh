#!/usr/bin/env bash
# Golden trajectory for the OracleAgent: the minimal, correct, targeted COMPOUND fix.
#
# Runs in the `main` operator-shell pod. Two independent faults:
#   LEG A (db) — the durable maintenance controller runs a real PostgreSQL
#   CHECKPOINT every 60 s at offset 35, inside the recurring write peak: every
#   send in that 8 s window stalls but succeeds. Repair = move the offset into the
#   trough, preserving enabled / period 60 / duration 8 (06-F4's repair).
#   LEG B (message) — the role's db pool is 3 + 2 = 5 while every peer runs 20/10:
#   at the write peak acquisition queues for seconds and sends time out; the
#   trough drains. Repair = restore the peer-uniform 20/10 through restart-svc.sh
#   (PUT /admin/config + POST /admin/reload + /healthz poll). Nothing above 20/10.
set -euo pipefail

MAINTENANCE_URL="http://db-maintenance:8081"
MSG_BASE="http://svc-message:8000"

echo "[solve] current durable maintenance state (expect offset 35, inside the peak):"
curl -fsS "${MAINTENANCE_URL}/v1/maintenance"
echo
echo "[solve] db pool config before (message vs a peer role):"
curl -fsS "${MSG_BASE}/admin/config"
echo
curl -fsS "http://svc-channel:8000/admin/config"
echo

# LEG A: preserve real checkpoint maintenance, its period and duration; move only
# the recurring offset from the write peak into the trough.
echo "[solve] moving the maintenance schedule out of the write peak:"
curl -fsS -X PUT "${MAINTENANCE_URL}/v1/maintenance" \
  -H 'content-type: application/json' \
  --data '{"enabled":true,"period_s":60,"offset_s":55,"duration_s":8}'
echo

# LEG B: restore the peer-uniform pool on svc-message through the supported helper.
echo "[solve] restoring the peer-uniform db pool on svc-message:"
echo '{"db":{"pool_size":20,"max_overflow":10}}' | restart-svc.sh message
echo "[solve] db pool config after (expect pool_size 20 / max_overflow 10):"
curl -fsS "${MSG_BASE}/admin/config"
echo

# ONE report naming BOTH (service, component) pairs.
submit_incident_report --help >/dev/null
submit_incident_report <<'JSON'
{"findings": [
  {
    "service": "db",
    "component": "db.maintenance-controller",
    "mechanism": "The durable database maintenance controller scheduled real PostgreSQL checkpoints every 60 seconds at offset 35 from the load epoch, so each 8-second run overlapped the recurring write peak and every send in that window stalled. I preserved enabled checkpoint maintenance with its period and duration and moved the complete schedule to offset 55 so future checkpoints execute in the trough."
  },
  {
    "service": "message",
    "component": "message.db-pool",
    "mechanism": "Independently, svc-message's database connection pool (pool_size 3 + max_overflow 2 = 5) was below peak concurrent demand while every peer role runs 20/10, so sends queued on connection acquisition for seconds and timed out at every peak and recovered in the trough. Restoring the peer-uniform pool_size 20 / max_overflow 10 through the supported config reload removed the bottleneck without inflating capacity."
  }
]}
JSON

echo "[solve] incident report filed; schedule moved to the trough, pool restored."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
