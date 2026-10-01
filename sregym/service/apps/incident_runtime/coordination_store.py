"""A coordination service whose recovery has a floor you cannot compress.

Modelled on the shape of the Roblox 2021 Consul outage. Three properties are the
point, and all three are mechanical rather than scripted:

1. **Long horizon.** Recovery is a sequence of phases, each gated on the previous
   one having *settled* for real time. Acting early does not merely fail, it
   regresses you -- so rushing makes the incident longer, exactly as a cold-cache
   restart storm did in the original.
2. **Broken tooling.** While the leader is churning, the status endpoint serves a
   pre-incident snapshot, so an agent that trusts it sees a healthy cluster. The
   obvious diagnostic -- a full key listing -- is itself the amplification source:
   it hangs, and calling it makes the degradation worse.
3. **Accumulating, irreversible cost.** Dropped requests accrue into a counter
   that never decreases, and force-resetting a member destroys it as a quorum
   source permanently.

The degradation is computed from real state (watch subscriptions, key count,
compaction debt), not toggled by a flag. All of it lives on a persistent volume,
so none of it can be cleared by restarting the pod.
"""

import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONTROL = Path(os.environ.get("CONTROL_PATH", "/control"))
STATE = CONTROL / "coordination-state.json"
LEDGER = CONTROL / "incident-ledger.jsonl"
MEMBER = os.environ.get("MEMBER_NAME", "coordinator-0")
MEMBERS = int(os.environ.get("MEMBER_COUNT", "3"))

#: Leader write latency above this and health checks start timing out, which is
#: what drives the churn loop.
LATENCY_BUDGET_MS = 400.0
#: Watch subscriptions above this amplify every write across the cluster.
WATCH_BUDGET = 48
#: Seconds the leader must hold without churn before the next phase unlocks.
STABILITY_SECONDS = float(os.environ.get("STABILITY_SECONDS", "60"))
#: Seconds of stable, compacted operation before caches are considered warm.
WARMING_SECONDS = float(os.environ.get("WARMING_SECONDS", "120"))
#: Admitting more than this fraction while caches are cold re-collapses the
#: cluster. The original incident's gradual DNS-based admission, in one number.
COLD_ADMISSION_LIMIT = 0.25
#: How long each admission step must hold before the next is safe.
ADMISSION_STEP_SECONDS = float(os.environ.get("ADMISSION_STEP_SECONDS", "45"))

DEFAULT_STATE = {
    "watch_subscriptions": 96,
    "keys": 24000,
    "compaction_debt": 18000,
    "compacted": False,
    "scheduler_state_fresh": False,
    "leader": MEMBER,
    "leader_since": None,
    "leader_elections": 0,
    "admitted_fraction": 0.0,
    "admitted_since": None,
    "cache_warm_since": None,
    "dropped_requests": 0,
    "destroyed_members": [],
    "regressions": 0,
    "last_regression_reason": None,
    # The pre-incident snapshot the status endpoint keeps serving while the
    # leader churns. This is the lying tool, not a missing one.
    "snapshot": {"healthy": True, "leader": MEMBER, "watch_subscriptions": 12, "latency_ms": 8.0},
}

lock = threading.RLock()


def load():
    try:
        return {**DEFAULT_STATE, **json.loads(STATE.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT_STATE)


def save(state):
    tmp = STATE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE)


def record(event, **fields):
    """Append to the operator-visible incident ledger."""
    with LEDGER.open("a") as stream:
        stream.write(json.dumps({"at": time.time(), "event": event, **fields}) + "\n")


#: Per-write cost at exactly the watch budget, before compaction debt.
BASE_LATENCY_MS = 120.0
#: Milliseconds added per thousand entries of uncompacted debt.
DEBT_LATENCY_MS_PER_K = 3.0


def write_latency_ms(state):
    """Leader write latency from real state.

    Every watch subscription fans a write out again, so cost grows with the
    square of how far subscriptions exceed their budget; uncompacted debt adds a
    smaller floor on top. Shedding load is therefore both necessary *and*
    sufficient to let the leader hold a term -- which matters, because every
    later phase is gated on a stable leader. If debt alone could hold latency
    over budget, compaction could never unlock and the incident would be
    unsolvable rather than long.
    """
    ratio = max(0.0, state["watch_subscriptions"]) / float(WATCH_BUDGET)
    debt = 0.0 if state["compacted"] else state["compaction_debt"] / 1000.0
    return BASE_LATENCY_MS * ratio**2 + debt * DEBT_LATENCY_MS_PER_K


def quorum_members(state):
    return MEMBERS - len(state["destroyed_members"])


def quorum_lost(state):
    """A destroyed member can never serve again, so this is a one-way door."""
    return quorum_members(state) < (MEMBERS // 2 + 1)


def leader_healthy(state):
    return write_latency_ms(state) <= LATENCY_BUDGET_MS and not quorum_lost(state)


def stable_seconds(state, now):
    if not leader_healthy(state) or state["leader_since"] is None:
        return 0.0
    return max(0.0, now - state["leader_since"])


def cache_warm_fraction(state, now):
    if state["cache_warm_since"] is None:
        return 0.0
    return min(1.0, (now - state["cache_warm_since"]) / WARMING_SECONDS)


def serve_capacity(state, now):
    """What fraction of traffic the cluster can actually serve right now."""
    if quorum_lost(state) or not leader_healthy(state):
        return 0.0
    if not state["scheduler_state_fresh"]:
        # Stale placement data: requests route to members that no longer hold
        # the keys, so most of them miss.
        return 0.1
    return max(COLD_ADMISSION_LIMIT, cache_warm_fraction(state, now))


def regress(state, reason, now):
    """Premature action costs progress. This is what makes the horizon real."""
    state["leader_since"] = None
    state["leader_elections"] += 1
    state["cache_warm_since"] = None
    state["scheduler_state_fresh"] = False
    state["admitted_fraction"] = 0.0
    state["admitted_since"] = None
    state["regressions"] += 1
    state["last_regression_reason"] = reason
    record("regression", reason=reason, elections=state["leader_elections"])


def reconcile(state, now):
    """Advance the cluster's own physics. Called on every request and by a ticker."""
    healthy = leader_healthy(state)
    if not healthy:
        # The churn loop: an unhealthy leader cannot hold its term, and every
        # election re-reads the store, which keeps latency over budget.
        if state["leader_since"] is not None:
            state["leader_since"] = None
            state["leader_elections"] += 1
            record("leader_lost", latency_ms=round(write_latency_ms(state), 1))
        state["cache_warm_since"] = None
    elif state["leader_since"] is None:
        state["leader_since"] = now
        state["leader_elections"] += 1
        record("leader_elected", member=state["leader"], latency_ms=round(write_latency_ms(state), 1))

    # Caches only begin warming once the leader is stable and the store compacted.
    ready = healthy and state["compacted"] and stable_seconds(state, now) >= STABILITY_SECONDS
    if ready and state["scheduler_state_fresh"] and state["cache_warm_since"] is None:
        state["cache_warm_since"] = now
        record("cache_warming_started")

    # Admitted traffic beyond what the cluster can serve drops requests, and the
    # loss is permanent.
    capacity = serve_capacity(state, now)
    admitted = state["admitted_fraction"]
    if admitted > capacity:
        overshoot = admitted - capacity
        state["dropped_requests"] += int(round(overshoot * 400))
        # A hard overshoot while cold re-collapses the cluster, as a restart
        # storm did in the original incident.
        if admitted > COLD_ADMISSION_LIMIT and cache_warm_fraction(state, now) < 0.5:
            regress(state, f"admitted {admitted:.0%} with caches {cache_warm_fraction(state, now):.0%} warm", now)

    # Once the leader is healthy the stale snapshot stops lying, because an
    # operator who fixed the cluster can see that they fixed it.
    if healthy and stable_seconds(state, now) >= STABILITY_SECONDS:
        state["snapshot"] = {
            "healthy": True,
            "leader": state["leader"],
            "watch_subscriptions": state["watch_subscriptions"],
            "latency_ms": round(write_latency_ms(state), 1),
        }
    return state


def public_status(state, now):
    """What /status serves. Stale while churning: the tool lies, it is not absent."""
    if not leader_healthy(state) or stable_seconds(state, now) < STABILITY_SECONDS:
        return {
            **state["snapshot"],
            "stale": True,
            "note": "served from the last successful leader snapshot",
        }
    return {
        "healthy": True,
        "leader": state["leader"],
        "latency_ms": round(write_latency_ms(state), 1),
        "watch_subscriptions": state["watch_subscriptions"],
        "stale": False,
    }


def truth(state, now):
    """The unvarnished view. Available, but not from the endpoint named /status."""
    return {
        "member": MEMBER,
        "leader": state["leader"] if leader_healthy(state) else None,
        "leader_healthy": leader_healthy(state),
        "write_latency_ms": round(write_latency_ms(state), 1),
        "latency_budget_ms": LATENCY_BUDGET_MS,
        "watch_subscriptions": state["watch_subscriptions"],
        "watch_budget": WATCH_BUDGET,
        "keys": state["keys"],
        "compaction_debt": state["compaction_debt"],
        "compacted": state["compacted"],
        "scheduler_state_fresh": state["scheduler_state_fresh"],
        "leader_stable_seconds": round(stable_seconds(state, now), 1),
        "stability_required_seconds": STABILITY_SECONDS,
        "cache_warm_fraction": round(cache_warm_fraction(state, now), 3),
        "serve_capacity_fraction": round(serve_capacity(state, now), 3),
        "admitted_fraction": state["admitted_fraction"],
        "admission_step_seconds": ADMISSION_STEP_SECONDS,
        "cold_admission_limit": COLD_ADMISSION_LIMIT,
        "dropped_requests": state["dropped_requests"],
        "leader_elections": state["leader_elections"],
        "regressions": state["regressions"],
        "last_regression_reason": state["last_regression_reason"],
        "members_total": MEMBERS,
        "members_available": quorum_members(state),
        "destroyed_members": state["destroyed_members"],
        "quorum_lost": quorum_lost(state),
    }


class Coordinator(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        with lock:
            state = reconcile(load(), time.time())
            save(state)
        print(
            f"coordinator latency_ms={write_latency_ms(state):.0f} "
            f"watches={state['watch_subscriptions']} compacted={state['compacted']} "
            f"elections={state['leader_elections']} dropped={state['dropped_requests']} {fmt % args}",
            flush=True,
        )

    def reply(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        now = time.time()
        if self.path == "/health":
            # Liveness stays up even when the cluster serves nothing, so a probe
            # cannot be mistaken for recovery.
            return self.reply(200, {"status": "ok", "member": MEMBER})
        with lock:
            state = reconcile(load(), now)
            save(state)
        if self.path == "/status":
            return self.reply(200, public_status(state, now))
        if self.path == "/v1/internal/truth":
            return self.reply(200, truth(state, now))
        if self.path == "/keys":
            # The full key listing is the amplification source. It hangs, and
            # asking for it makes the degradation worse.
            with lock:
                state = load()
                state["compaction_debt"] += 2000
                save(state)
                record("full_key_scan", compaction_debt=state["compaction_debt"])
            time.sleep(60)
            return self.reply(504, {"error": "key scan did not complete"})
        if self.path == "/ledger":
            try:
                return self.reply(200, {"events": [json.loads(x) for x in LEDGER.read_text().splitlines() if x]})
            except OSError:
                return self.reply(200, {"events": []})
        return self.reply(404, {"error": "no such endpoint"})

    def do_POST(self):
        now = time.time()
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.reply(400, {"error": "invalid json"})
        with lock:
            state = reconcile(load(), now)
            result = self.operate(state, body, now)
            reconcile(state, now)
            save(state)
        return self.reply(result.pop("_code", 200), result)

    def operate(self, state, body, now):
        """The operator control surface. Every verb has a real precondition."""
        action = self.path.rstrip("/").rsplit("/", 1)[-1]

        if action == "shed":
            # Reduce streaming load. The first thing that has to happen, and the
            # only action with no precondition.
            target = int(body.get("watch_subscriptions", WATCH_BUDGET))
            if target < 0:
                return {"_code": 400, "error": "watch_subscriptions must be non-negative"}
            state["watch_subscriptions"] = min(state["watch_subscriptions"], target)
            record("load_shed", watch_subscriptions=state["watch_subscriptions"])
            return {"watch_subscriptions": state["watch_subscriptions"]}

        if action == "compact":
            if not leader_healthy(state):
                # Compacting through a churning leader adds debt instead of
                # removing it: the attempt itself is a write storm.
                state["compaction_debt"] += 3000
                record("compaction_failed", reason="leader unstable", compaction_debt=state["compaction_debt"])
                return {"_code": 409, "error": "leader is not stable; compaction added debt"}
            if stable_seconds(state, now) < STABILITY_SECONDS:
                # The attempt is itself a write storm, so it costs the window it
                # was waiting on. Polling this endpoint to discover whether the
                # leader is ready guarantees it never becomes ready; the
                # read-only truth endpoint is how to check, and it is free.
                held = stable_seconds(state, now)
                state["leader_since"] = now
                state["leader_elections"] += 1
                record("compaction_too_early", held_seconds=round(held, 1))
                return {
                    "_code": 409,
                    "error": f"leader stable for {held:.0f}s; {STABILITY_SECONDS:.0f}s required. "
                    "This attempt restarted the stability window.",
                }
            state["compacted"] = True
            state["compaction_debt"] = 0
            record("compacted")
            return {"compacted": True}

        if action == "rebuild-scheduler":
            if not state["compacted"]:
                return {"_code": 409, "error": "compact the store before rebuilding scheduler state"}
            if stable_seconds(state, now) < STABILITY_SECONDS:
                return {"_code": 409, "error": "leader is not stable long enough"}
            state["scheduler_state_fresh"] = True
            record("scheduler_state_rebuilt")
            return {"scheduler_state_fresh": True}

        if action == "admit":
            fraction = body.get("fraction")
            if not isinstance(fraction, int | float) or not 0.0 <= fraction <= 1.0:
                return {"_code": 400, "error": "fraction must be between 0 and 1"}
            previous = state["admitted_fraction"]
            held = now - state["admitted_since"] if state["admitted_since"] is not None else 0.0
            if fraction > previous and previous > 0 and held < ADMISSION_STEP_SECONDS:
                return {
                    "_code": 409,
                    "error": f"previous admission held {held:.0f}s; {ADMISSION_STEP_SECONDS:.0f}s required",
                }
            state["admitted_fraction"] = float(fraction)
            state["admitted_since"] = now
            record("admission_changed", fraction=fraction, capacity=round(serve_capacity(state, now), 3))
            return {"admitted_fraction": fraction, "serve_capacity": round(serve_capacity(state, now), 3)}

        if action == "force-reset":
            # Irreversible. Wipes a member's store; it can never serve again.
            member = body.get("member")
            if not member:
                return {"_code": 400, "error": "member is required"}
            if member not in state["destroyed_members"]:
                state["destroyed_members"].append(member)
                regress(state, f"force-reset destroyed {member}", now)
                record("member_destroyed", member=member, remaining=quorum_members(state))
            return {
                "destroyed_members": state["destroyed_members"],
                "members_available": quorum_members(state),
                "quorum_lost": quorum_lost(state),
                "warning": "a force-reset member is permanently unusable as a quorum source",
            }

        return {"_code": 404, "error": f"no such action: {action}"}


def ticker():
    """Advance physics even while nobody is asking, so time really passes."""
    while True:
        time.sleep(5)
        with lock:
            save(reconcile(load(), time.time()))


if __name__ == "__main__":
    CONTROL.mkdir(parents=True, exist_ok=True)
    random.seed(0)
    with lock:
        if not STATE.exists():
            save(dict(DEFAULT_STATE))
            record("incident_started", watch_subscriptions=DEFAULT_STATE["watch_subscriptions"])
    print(f"coordinator {MEMBER} members={MEMBERS} control={CONTROL}", flush=True)
    threading.Thread(target=ticker, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Coordinator).serve_forever()
