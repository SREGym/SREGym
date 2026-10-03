#!/usr/bin/env bash
# Golden trajectory for the OracleAgent — ONE TRIGGER, TWO LATENT DEFECTS.
#
#   A — TRIGGER (runtime, timed). At t=40 s of the load epoch the generator
#   activated `read_consistency_strict` on svc-channel. From that instant
#   /authz/resolve stops answering the send path from its in-process membership
#   cache and takes a POOLED connection on every call (channel.ts:149-163). The
#   event set is NEVER in /admin/config — only GET /admin/event shows it. Repair =
#   ONE PUT setting the same event inactive. No restart: the event lives in the
#   process, so restart-svc.sh (PUT /admin/config + POST /admin/reload) cannot
#   change it.
#   B — LATENT #1 (config, static). svc-channel's own db pool is 3 + 2 = 5 while
#   every peer role runs 20/10. It is DORMANT until A opens the resolve path —
#   231.9 / 233.1 ms p90 on its own — and under A it caps the resolve rate at
#   ~14.3/s against a 50 rps peak, so the pool is exhausted, acquisitions time out
#   and /authz/resolve returns 503 `authz_unavailable` (channel.ts:163-166),
#   failing every send. Repair = restore the peer-uniform 20/10 through
#   restart-svc.sh. Nothing above 20/10.
#   C — LATENT #2 (runtime, on a DIFFERENT service). At the same t=40 s the
#   generator activated `store_consistency_strict` on svc-AUTH, so every Redis
#   operation that role issues awaits STORE_HOLD_MS (250) before returning
#   (redis.ts:19-35). Every send validates its session on svc-auth (AUTH_CHECK=1,
#   message.ts:297-306) and pays that hold; sign-in pays it too. It is QUIET while
#   A+B are unrepaired — a send path returning 503s is not measuring +250 ms — and
#   it becomes the whole remaining gap once they are fixed. Repair = ONE PUT on
#   svc-auth's /admin/event. It never auto-reverts, so the verifier-owned
#   post-freeze state probe rejects leaving it armed even when generic SLIs pass.
#
# ALL THREE must be repaired and all three must be reported. Clearing A alone
# leaves B, which the pool floors in repair_scope catch on the spot and which the
# soak-end pool snapshot checks again. Clearing A and repairing B but leaving C is
# the OTHER half-fix — the one that can look healthy after central SLI tolerance —
# and the protected event-state probe rejects it directly.
#
# TIMING — A and C are both TIMED, so the golden must OBSERVE them before it can
# repair them. Both land at t=40 s and the oracle starts at t~0, so a PUT issued
# straight away deactivates an event that is not active yet: a no-op the generator
# then undoes at t=40, inside the graded soak (the soak begins at
# max(declare_ts, warmup 30 s), runner._resolve_soak_start). Poll the same
# endpoints an agent would until the events are really there, then revert them.
set -euo pipefail

CHAN_BASE="http://svc-channel:8000"
AUTH_BASE="http://svc-auth:8000"
MSG_BASE="http://svc-message:8000"
EVENT_WAIT_S=120

echo "[solve] db pool config before (channel vs a peer role — 3/2 against 20/10):"
curl -fsS "${CHAN_BASE}/admin/config"
echo
curl -fsS "${MSG_BASE}/admin/config"
echo
echo "[solve] runtime event state before (the event sets do NOT appear in"
echo "        /admin/config above; both pushes land at t=40 s):"
printf '[solve]   svc-channel /admin/event: '
curl -fsS "${CHAN_BASE}/admin/event" || true
echo
printf '[solve]   svc-auth    /admin/event: '
curl -fsS "${AUTH_BASE}/admin/event" || true
echo

# Wait for the timed pushes to actually land, bounded so a genuinely missing event
# fails loudly here instead of as an unexplained outcome miss 150 s later. Both are
# episode-anchored at the same t, so one bounded wait per role is enough.
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
echo "[solve] channel push observed after ~${SECONDS}s: ${events}"

echo "[solve] waiting up to ${EVENT_WAIT_S}s for store_consistency_strict on svc-auth:"
deadline=$((SECONDS + EVENT_WAIT_S))
while :; do
  auth_events="$(curl -fsS "${AUTH_BASE}/admin/event" 2>/dev/null || true)"
  case "${auth_events}" in *store_consistency_strict*) break ;; esac
  if [ "${SECONDS}" -ge "${deadline}" ]; then
    echo "[solve] FATAL: store_consistency_strict never activated on svc-auth;" >&2
    echo "[solve] last /admin/event body: ${auth_events}" >&2
    exit 1
  fi
  sleep 2
done
echo "[solve] auth push observed after ~${SECONDS}s: ${auth_events}"

# A: turn the pushed consistency toggle back off on svc-channel. One action.
echo "[solve] A (trigger): deactivating read_consistency_strict on svc-channel:"
curl -fsS -X PUT "${CHAN_BASE}/admin/event" \
  -H 'content-type: application/json' \
  --data '{"name":"read_consistency_strict","active":false}'
echo

# C: return the shared store to its normal operating policy on svc-auth. One action,
# and the one an agent that stops at "sends are healthy again" never takes.
echo "[solve] C (latent #2): deactivating store_consistency_strict on svc-auth:"
curl -fsS -X PUT "${AUTH_BASE}/admin/event" \
  -H 'content-type: application/json' \
  --data '{"name":"store_consistency_strict","active":false}'
echo

# B: restore the peer-uniform pool on svc-channel through the supported helper.
echo "[solve] B (latent #1): restoring the peer-uniform db pool on svc-channel:"
echo '{"db":{"pool_size":20,"max_overflow":10}}' | restart-svc.sh channel

echo "[solve] confirming all three repairs:"
echo "[solve] svc-channel events after (expect read_consistency_strict absent/inactive):"
curl -fsS "${CHAN_BASE}/admin/event"
echo
echo "[solve] svc-auth events after (expect store_consistency_strict absent/inactive):"
curl -fsS "${AUTH_BASE}/admin/event"
echo
echo "[solve] svc-channel db pool after (expect pool_size 20 / max_overflow 10):"
curl -fsS "${CHAN_BASE}/admin/config"
echo

# FAIL LOUDLY if either clear did not take: a half-repaired golden would be graded
# as the healthy reference and poison the fence.
for probe in "${CHAN_BASE}|read_consistency_strict" "${AUTH_BASE}|store_consistency_strict"; do
  base="${probe%%|*}"; name="${probe##*|}"
  body="$(curl -fsS "${base}/admin/event" 2>/dev/null || true)"
  case "${body}" in
    *"${name}"*)
      echo "[solve] FATAL: ${name} is still active on ${base}" >&2
      echo "[solve] last /admin/event body: ${body}" >&2
      exit 1
      ;;
  esac
done
echo "[solve] both runtime events cleared."

# ONE report naming ALL THREE (service, component) pairs.
submit_incident_report --help >/dev/null
submit_incident_report <<'JSON'
{"findings": [
  {
    "service": "channel",
    "component": "channel.membership-acl",
    "mechanism": "A runtime consistency toggle pushed onto the channel service partway through the episode made its channel-to-org membership/ACL resolve bypass the in-process cache and re-query the database on every send, taking one of the channel service's own database connections per resolve. Because the send path resolves authorization before persisting, sends began failing from that instant on. I turned the same runtime toggle back off on the channel service, which restored the cached resolve immediately without a restart or a configuration edit."
  },
  {
    "service": "channel",
    "component": "channel.db-pool",
    "mechanism": "Independently, the channel service's database connection pool (pool_size 3 + max_overflow 2 = 5) was far below the peer-uniform 20/10 every other role runs, and far below what the send path needs once every authorization resolve holds a connection for the length of a lookup. That pool is never touched while authorization resolves from cache, so it was invisible until the toggle opened the database path; from then on five connections could not sustain the send rate, acquisitions queued and timed out, and the authorization resolve returned 503 authz_unavailable, failing sends outright. Restoring the peer-uniform pool_size 20 / max_overflow 10 through the supported config reload removed the bottleneck without inflating capacity."
  },
  {
    "service": "redis",
    "component": "redis.cache-policy",
    "mechanism": "Independently again, and at the same moment, the shared key-value store that backs auth sessions was switched into a degraded strict-consistency operating policy on the auth service, so every operation that service issues against the store paid a fixed quarter-second hold before returning. The send path validates its session on the auth service before it persists anything, so every send paid that hold on top of the channel-side cost, and signing in paid it too. While sends were failing outright it was hidden; once the channel side was repaired it was the entire remaining gap between the send path and its published limit. Returning the store to its normal operating policy on the auth service cleared it immediately, with no restart and no configuration edit."
  }
]}
JSON

echo "[solve] incident report filed; channel toggle reverted, channel pool restored, shared-store strict mode cleared on auth."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
