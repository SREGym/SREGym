#!/usr/bin/env bash
# Golden trajectory for the OracleAgent — TRIGGER + LATENT compound.
#
#   A — TRIGGER (runtime, timed). At t=40 s of the load epoch the generator
#   activated `read_consistency_strict` on svc-channel. From that instant
#   /authz/resolve stops answering the send path from its in-process membership
#   cache and takes a POOLED connection on every call (channel.ts:149-163). The
#   event set is NEVER in /admin/config — only GET /admin/event shows it. Repair =
#   ONE PUT setting the same event inactive. No restart: the event lives in the
#   process, so restart-svc.sh (PUT /admin/config + POST /admin/reload) cannot
#   change it.
#   B — LATENT (config, static). svc-channel's own db pool is 16 + 4 = 20 while
#   every peer role runs 20/10 = 30. It is DORMANT until A opens the resolve path — a
#   cached resolve takes NO pooled connection at ANY pool size (channel.ts:149-163),
#   which is a mechanism fact, not an extrapolation. Under A the 20 connections support
#   ~57 resolves/s against a 50 rps peak: a NOMINAL SURPLUS, so on this rung the pool
#   may not move the send band at all, and the deterministic grading of B is the
#   repair-state floor (max_overflow >= 5 against the injected 4). Repair = restore the
#   peer-uniform 20/10 through restart-svc.sh. Nothing above 20/10.
#
#   16/4 is the THIRD RUNG of the realism ladder (3/2 = 5, then 12/2 = 14, now 20). It is
#   plausible AND nominally sufficient, so the golden prints the channel pool next to a
#   peer's and the reasoning that finds it is headroom-against-a-bursty-demand, not
#   spot-the-odd-number and not steady-state arithmetic.
#
# BOTH must be repaired. Clearing A alone restores a healthy-LOOKING send band and
# is exactly the half-fix this task is built to catch: the pool floors in
# repair_scope fail on the spot, and the same event fires AGAIN 60 s after the
# declaration, collapsing the graded soak.
#
# TIMING — A is TIMED, so the golden must OBSERVE it before it can repair it. The
# push lands at t=40 s and the oracle starts at t~0, so a PUT issued straight away
# deactivates an event that is not active yet: a no-op the generator then undoes
# at t=40, inside the graded soak (the soak begins at max(declare_ts, warmup 30 s),
# runner._resolve_soak_start). Poll the same endpoint an agent would until the
# event is really there, then revert it.
set -euo pipefail

CHAN_BASE="http://svc-channel:8000"
MSG_BASE="http://svc-message:8000"
EVENT_WAIT_S=120
ADMIN_WAIT_S=60

# The environment healthcheck proves svc-message once before episode-start. The
# write load starts immediately afterward, so a service can be between transient
# startup restarts when the oracle begins. These reads are diagnostic only; retry
# them instead of letting one connect race abort the actual repair.
read_admin_config() {
  local base="$1"
  local deadline=$((SECONDS + ADMIN_WAIT_S))
  local body=""
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    if body="$(curl -fsS -m 3 "${base}/admin/config" 2>/dev/null)"; then
      printf '%s\n' "${body}"
      return 0
    fi
    sleep 2
  done
  echo "[solve] FATAL: ${base}/admin/config stayed unreachable for ${ADMIN_WAIT_S}s" >&2
  return 1
}

echo "[solve] db pool config before (channel vs a peer role — 16/4 against 20/10):"
read_admin_config "${CHAN_BASE}"
read_admin_config "${MSG_BASE}"
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

# A: turn the pushed consistency toggle back off on svc-channel. One action.
echo "[solve] A (trigger): deactivating read_consistency_strict on svc-channel:"
curl -fsS -X PUT "${CHAN_BASE}/admin/event" \
  -H 'content-type: application/json' \
  --data '{"name":"read_consistency_strict","active":false}'
echo

# B: restore the peer-uniform pool on svc-channel through the supported helper.
# This is the repair the outage-time evidence — the 503 `authz_unavailable`
# stream, the channel latency histogram, and GET /admin/config showing channel at
# 12/2 against every peer at 20/10 — points to, and it must be made even though
# the send band already looks healthy once A is off.
echo "[solve] B (latent): restoring the peer-uniform db pool on svc-channel:"
echo '{"db":{"pool_size":20,"max_overflow":10}}' | restart-svc.sh channel

echo "[solve] confirming both repairs:"
echo "[solve] svc-channel events after (expect read_consistency_strict absent/inactive):"
curl -fsS "${CHAN_BASE}/admin/event"
echo
echo "[solve] svc-channel db pool after (expect pool_size 20 / max_overflow 10):"
curl -fsS "${CHAN_BASE}/admin/config"
echo

# ONE report naming BOTH (service, component) pairs.
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
    "mechanism": "Independently, the channel service's database connection pool (pool_size 16 + max_overflow 4 = 20) was a third below the peer-uniform 20/10 every other role runs, and left almost no headroom over what the send path needs once every authorization resolve holds a connection for the length of a lookup. That pool is never touched while authorization resolves from cache, so it was invisible until the toggle opened the database path; from then on an ordinary arrival burst queued on the pool instead of being absorbed. Restoring the peer-uniform pool_size 20 / max_overflow 10 through the supported config reload removed the shortfall without inflating capacity above the other roles."
  }
]}
JSON

echo "[solve] incident report filed; runtime toggle reverted, channel pool restored."

# End the episode. `submit_incident_report` above is advisory and does not stop
# the clock, so this is what freezes the system and starts the graded soak.
declare_repair_complete
