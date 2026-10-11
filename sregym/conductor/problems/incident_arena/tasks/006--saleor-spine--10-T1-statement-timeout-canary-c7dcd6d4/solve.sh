#!/usr/bin/env bash
set -Eeuo pipefail

psql "$DB_ADMIN_DSN" -v ON_ERROR_STOP=1 -c \
  "ALTER ROLE saleor_app IN DATABASE saleor RESET statement_timeout"
# Role/database startup settings are copied only when a connection opens. Reap
# only the affected application role so Saleor reconnects in place; PostgreSQL,
# Saleor, the grading connection, and unrelated roles stay untouched.
psql "$DB_ADMIN_DSN" -v ON_ERROR_STOP=1 -tAc \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'saleor_app' AND pid <> pg_backend_pid()" >/dev/null
PGPASSWORD=agentrepair-app psql -h postgres -U saleor_app -d saleor \
  -v ON_ERROR_STOP=1 -tAc "SHOW statement_timeout" | grep -qx '0'
submit_incident_report \
  --service postgres \
  --component postgres.config \
  --mechanism "A role/database statement budget applied only to fresh Saleor application sessions and canceled heavier peak checkouts; removing that scoped startup override and replacing affected sessions restored the inherited policy."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
