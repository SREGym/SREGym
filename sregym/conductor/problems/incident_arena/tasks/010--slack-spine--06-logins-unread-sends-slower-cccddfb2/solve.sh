#!/usr/bin/env bash
# Golden trajectory for the OracleAgent — ONE OUTAGE, THREE VICTIMS.
#
# THE CAUSE. At t=40 s of the load epoch the generator activates
# `store_consistency_strict` on svc-auth, svc-workspace AND svc-notification (one
# admin_event per role, runner._fire_admin_event -> PUT /admin/event). From that instant
# every Redis operation on those three roles awaits STORE_HOLD_MS (100 — the
# smaller dose that makes this the second rung of the ladder) before it returns
# (redis.ts:19-35). Three user journeys degrade together:
#   session_login  -> auth POST /login                            (auth.ts:42)   1 hold
#   session_post   -> auth POST /validate                         (auth.ts:54)   2 holds
#                     + channel->workspace GET /orgs/:id/settings (workspace.ts:110)
#   session_unread -> notification GET /unread                    (notification.ts:51) 1 hold
# while every role's own db pool (including the message role's) stays at the peer-uniform
# 20/10. The send path is a VICTIM, not the cause.
#
# THE REPAIR. The event set is NEVER in /admin/config — only GET /admin/event shows it.
# One PUT per affected role setting the same event inactive; the active set is a live
# module-level Set that redisStoreHold() reads per operation (event.ts:88-96), so the
# clear takes effect immediately with no restart and no config edit. It is DURABLE here
# because the arming is a runtime LoadEvent, not the container env: nothing re-arms it.
#
# TIMING. The push is TIMED, so the golden must OBSERVE it before it can repair it. A PUT
# issued at t~0 would deactivate an event that is not active yet — a no-op the generator
# then undoes at t=40, inside the graded soak. That matters MORE at this rung's small dose:
# the lanes move by 100-200 ms rather than 250-500, so a golden that repaired nothing would
# not be obvious from the band alone. Poll the same endpoint an agent would until the event
# is really there, then revert it on all three roles.
set -euo pipefail

ROLES="auth workspace notification"
EVENT_WAIT_S=120

echo "[solve] runtime event state BEFORE (the fault is here; it does NOT appear in"
echo "[solve] /admin/config on any role):"
for role in $ROLES message channel; do
  printf '[solve]   svc-%s /admin/event: ' "$role"
  curl -fsS "http://svc-${role}:8000/admin/event" || true
  echo
done
echo
echo "[solve] message role db pool (expected healthy, peer-uniform 20/10 — the send path"
echo "[solve] is a VICTIM of the shared store, not a pool problem):"
curl -fsS "http://svc-message:8000/admin/config" || true
echo

# Wait for the timed push to actually land, bounded so a genuinely missing event fails
# loudly HERE instead of as an unexplained outcome miss 150 s later. svc-auth is polled
# because the login lane is the first to visibly degrade; all three fire at the same t.
echo "[solve] waiting up to ${EVENT_WAIT_S}s for store_consistency_strict on svc-auth:"
deadline=$((SECONDS + EVENT_WAIT_S))
while :; do
  events="$(curl -fsS "http://svc-auth:8000/admin/event" 2>/dev/null || true)"
  case "${events}" in *store_consistency_strict*) break ;; esac
  if [ "${SECONDS}" -ge "${deadline}" ]; then
    echo "[solve] FATAL: store_consistency_strict never activated on svc-auth;" >&2
    echo "[solve] last /admin/event body: ${events}" >&2
    exit 1
  fi
  sleep 2
done
echo "[solve] push observed after ~${SECONDS}s: ${events}"

# THE REPAIR: clear the shared-store strict-consistency mode on every role that consults
# it. One PUT each, no restart, no configuration edit.
for role in $ROLES; do
  echo "[solve] deactivating store_consistency_strict on svc-${role}:"
  curl -fsS -X PUT "http://svc-${role}:8000/admin/event" \
    -H 'content-type: application/json' \
    --data '{"name":"store_consistency_strict","active":false}'
  echo
done
echo

echo "[solve] runtime event state AFTER (expect an EMPTY active set on all three):"
for role in $ROLES; do
  printf '[solve]   svc-%s /admin/event: ' "$role"
  curl -fsS "http://svc-${role}:8000/admin/event" || true
  echo
done
# FAIL LOUDLY if any clear did not take: a half-repaired golden would be graded as the
# healthy reference and poison the fence.
for role in $ROLES; do
  events="$(curl -fsS "http://svc-${role}:8000/admin/event" 2>/dev/null || true)"
  case "${events}" in
    *store_consistency_strict*)
      echo "[solve] FATAL: store_consistency_strict is still active on svc-${role}" >&2
      echo "[solve] last /admin/event body: ${events}" >&2
      exit 1
      ;;
  esac
  case "${events}" in
    *'"active"'*'[]'*) : ;;
    *)
      echo "[solve] FATAL: svc-${role} did not report an empty active set: ${events}" >&2
      exit 1
      ;;
  esac
done
echo "[solve] all three roles report active []."
echo

# ONE report naming the SINGLE (service, component) pair: the shared store's operating
# policy, not any one of the three services that surface the symptom. Naming a victim role
# as well fails canonical_report_set — over-attribution is a miss on this shape.
submit_incident_report --help >/dev/null
submit_incident_report <<'JSON'
{"findings": [
  {
    "service": "redis",
    "component": "redis.cache-policy",
    "mechanism": "The shared key-value store that backs auth sessions, the workspace settings cache and the notification unread counters was switched into a degraded strict-consistency operating policy partway through the day, so every operation against it paid a fixed hold of about a tenth of a second before returning. Three services depend on that one store, so they slowed together: sign-in waited on the session write, message sends waited twice (once on the session validate and once on the channel-to-workspace settings read, which is why the send lane moved by about twice as much as the other two), and unread checks waited on the counter read, while every service's own database pool and per-service cache stayed healthy and the message role's pool was never saturated. Returning the shared store to its normal operating policy on each dependent service cleared all three lanes at once, with no restart and no configuration edit."
  }
]}
JSON

echo "[solve] incident report filed; shared-store strict mode cleared on auth, workspace and notification."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
