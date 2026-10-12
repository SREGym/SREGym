#!/usr/bin/env bash
set -euo pipefail

MAINTENANCE_URL="http://db-maintenance:8081"

echo "[solve] current durable maintenance state"
curl -fsS "${MAINTENANCE_URL}/v1/maintenance"
echo

# Preserve real checkpoint maintenance, its fixed period, and workload duration.
# Move only the recurring offset from the write peak into the trough.
curl -fsS -X PUT "${MAINTENANCE_URL}/v1/maintenance" \
  -H 'content-type: application/json' \
  --data '{"enabled":true,"period_s":60,"offset_s":55,"duration_s":8}'
echo

submit_incident_report --help >/dev/null
submit_incident_report <<'JSON'
{
  "service": "db",
  "component": "db.maintenance-controller",
  "mechanism": "The durable database maintenance controller scheduled real PostgreSQL checkpoints every 60 seconds at offset 35 from the load epoch, so each run overlapped the recurring write peak. I preserved enabled checkpoint maintenance and its period and duration, then moved the complete schedule to offset 55 so future checkpoints execute in the trough."
}
JSON

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
