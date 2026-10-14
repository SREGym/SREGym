#!/usr/bin/env bash
set -euo pipefail

base=http://svc-message:8000

curl -fsS -X PUT "$base/admin/checkpoint" \
  -H 'Content-Type: application/json' \
  --data '{"mode":"request"}'

before="$(curl -fsS "$base/internal/s13/cp")"
before_starts="$(printf '%s' "$before" | python3 -c 'import json,sys; print(json.load(sys.stdin)["starts"])')"

# This scenario's in-band reload performs a real process restart. Wait for the
# boot counter to advance so a fast pre-exit health response cannot mask it.
restart-svc.sh message
deadline=$(( $(date +%s) + 120 ))
while :; do
  state="$(curl -fsS "$base/internal/s13/cp" 2>/dev/null || true)"
  if [ -n "$state" ]; then
    after_starts="$(printf '%s' "$state" | python3 -c 'import json,sys; print(json.load(sys.stdin)["starts"])' 2>/dev/null || true)"
    if [ -n "$after_starts" ] && [ "$after_starts" -gt "$before_starts" ]; then
      break
    fi
  fi
  [ "$(date +%s)" -lt "$deadline" ] || {
    echo "solve: svc-message process restart did not advance its boot counter" >&2
    exit 1
  }
  sleep 1
done

printf '%s\n' "$state" | grep -q '"mode":"request"' || {
  echo "solve: runtime rollback did not survive restart: $state" >&2
  exit 1
}

holders="$(psql "$DB_ADMIN_DSN" -tAc "SELECT count(DISTINCT a.pid) FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid JOIN pg_class c ON c.oid=l.relation WHERE a.state='idle in transaction' AND c.relname='channel_seq' AND a.query ~* 'SELECT[[:space:]]+last_seq[[:space:]]+FROM[[:space:]]+channel_seq.*FOR[[:space:]]+UPDATE';")"
[ "$holders" -eq 0 ] || { echo "solve: causal channel_seq holder remains: $holders" >&2; exit 1; }

submit_incident_report --help >/dev/null
submit_incident_report \
  --service message \
  --component message.sequencer \
  --mechanism "A session-scoped sequencer handoff retained a retry-reused DB lease and left one seeded cohort's channel_seq row locked in an idle transaction. Its writes queued and exhausted the shared pool. Persisted the narrow request-scoped handoff mode, restarted the service, verified the request mode survived boot, the row lock stayed absent, and messaging recovered."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
