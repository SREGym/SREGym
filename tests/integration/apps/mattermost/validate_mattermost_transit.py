"""Lifecycle checks for the Mattermost transit cascade before any agent screen.

Each control starts from a freshly cascaded incident, applies one response and
grades it with the mitigation oracle. The negative controls are the point: the
fixes a responder reaches for first must fail on data, and only a repair that
covers both the provisioner and the scale-in policy may pass -- whichever way
it covers them.

Tier 1b runs tier 1's controls; going around the provider network now fails
on customers too, since the backend no longer accepts the gateway directly, and
``direct_path`` checks that from inside a gateway pod.

Tier 2 has its own controls: two fleets, the chat edge and the push relay,
share one provisioner and one instance quota, and every phase judges chat
served and notifications delivered. Repairs that cover only one fleet, keep the
instances stuck under the quota, or switch notifications off must fail.

Run on a cluster with CloudNativePG, separately from any benchmark attempt. A
full run takes well over an hour; ``--only`` runs a subset. Hidden-namespace
checks need the conductor's agent proxy and are in ``--visibility``.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

from sregym.conductor.problems.apps.mattermost.transit_saturation import (
    REPAIRED_FLEET_POLICY,
    REPAIRED_OPEN_FILES,
    REPAIRED_SCALE_IN,
    MattermostTransitSaturationProblem,
)
from sregym.service.apps.mattermost.transit_cascade import PROVIDER_NAMESPACE
from sregym.service.apps.mattermost.transit_fleets import EDGE_CONNECTIONS, PUSH_REPLICAS

DEGRADED_FAILURES = {
    "gateway_instances_stuck_provisioning",
    "gateway_capacity_below_floor",
    "customers_not_served_while_degraded",
}
WITHOUT_AUTOMATION_FAILURES = {
    "gateway_capacity_below_floor_after_recovery",
    "customers_not_served_after_recovery",
    "gateway_latency_unresolved",
    "capacity_not_restored_by_automation",
    "replacement_instances_stuck_provisioning",
}


TIER2_DEGRADED_FAILURES = {"chat_not_served_while_degraded", "pushes_not_delivered_while_degraded"}
TIER2_LATER_FAILURES = {
    "chat_not_served_after_recovery",
    "pushes_not_delivered_after_recovery",
    "gateway_latency_unresolved",
    "chat_not_served_after_instance_loss",
    "pushes_not_delivered_after_instance_loss",
}
TIER2_FAILURES = TIER2_DEGRADED_FAILURES | TIER2_LATER_FAILURES


def calibrated_policy(app, group="chat-gateway"):
    """Tier 2 keeps one calibrated policy per fleet, under the fleet's name."""
    key = group if app.instance_quota else "policy"
    return app.command("get", "configmap", "capacity-calibrated", "-o", f"jsonpath={{.data.{key}}}")


def delete_stuck_pods(app, groups=("chat-gateway",)):
    pods = json.loads(app.command("get", "pods", "-l", f"app in ({','.join(groups)})", "-o", "json"))["items"]
    stuck = [
        p["metadata"]["name"]
        for p in pods
        if any(not s.get("ready") for s in p.get("status", {}).get("initContainerStatuses", []))
    ]
    if stuck:
        app.command("delete", "pod", *stuck, "--ignore-not-found", "--wait=false")
    return stuck


def delete_automation(app):
    app.command("delete", "deployment", "capacity-scaler", "--wait=true")
    app.command("wait", "--for=delete", "pod", "-l", "app=capacity-scaler", "--timeout=120s", timeout=150)


def settle(app):
    for name in app.cascade_deployments:
        app.command("rollout", "status", f"deployment/{name}", "--timeout=600s", timeout=630)


# ---- responses ---------------------------------------------------------
# Each takes (problem, app) and leaves the response in place for grading.


def no_op(problem, app):
    return


def manual_scale_up(problem, app):
    app.command("scale", "deployment/chat-gateway", f"--replicas={app.fleet_max}")


def raise_max(problem, app):
    app.scaler_policy(max=2 * app.fleet_max)


def provisioner_only(problem, app):
    problem.raise_provisioner_open_files(REPAIRED_OPEN_FILES)
    app.command("rollout", "status", "deployment/provisioner", "--timeout=300s", timeout=330)
    delete_stuck_pods(app)


def scale_in_only(problem, app):
    app.scaler_policy(min=app.capacity_floor)


def automation_deleted(problem, app):
    problem.raise_provisioner_open_files(REPAIRED_OPEN_FILES)
    app.command("rollout", "status", "deployment/provisioner", "--timeout=300s", timeout=330)
    delete_automation(app)
    delete_stuck_pods(app)
    app.command("scale", "deployment/chat-gateway", f"--replicas={app.fleet_max}")


def bypass(problem, app):
    problem.raise_provisioner_open_files(REPAIRED_OPEN_FILES)
    app.scaler_policy(min=app.capacity_floor)
    app.command("set", "env", "deployment/chat-gateway", "-c", "gateway", "GATEWAY_UPSTREAM=http://mattermost:8065")
    app.command("rollout", "status", "deployment/chat-gateway", "--timeout=600s", timeout=630)


def restart_and_saturation_scale_in(problem, app):
    """An alternative valid repair, different in both halves from the reference.

    Restarting releases the provisioner's descriptors without raising its limit,
    and scaling in on saturation stops reading blocked workers as idle; the floor
    still has to be the policy minimum, as the written policy states it.
    """
    app.command("rollout", "restart", "deployment/provisioner")
    app.command("rollout", "status", "deployment/provisioner", "--timeout=300s", timeout=330)
    app.scaler_policy(min=app.capacity_floor, scale_in={"metric": "saturation", "below": 20})
    delete_stuck_pods(app)


def reference(problem, app):
    problem.raise_provisioner_open_files(REPAIRED_OPEN_FILES)
    app.command("rollout", "status", "deployment/provisioner", "--timeout=300s", timeout=330)
    app.scaler_policy(min=app.capacity_floor)
    delete_stuck_pods(app)


def tier1_repair_keeping_stuck_instances(problem, app):
    """What every Tier 1 screen run did, short of replacing the stuck instances.

    Restarting stands in for the agents' source patch of the descriptor leak.
    """
    problem.raise_provisioner_open_files(REPAIRED_OPEN_FILES)
    app.command("rollout", "status", "deployment/provisioner", "--timeout=300s", timeout=330)
    app.scaler_policy(min=app.capacity_floor, max=2 * app.fleet_max, scale_in={"metric": "saturation", "below": 30})


def replace_stuck_instances(problem, app):
    """The Tier 1 repair plus replacing what is stuck: the places come back."""
    tier1_repair_keeping_stuck_instances(problem, app)
    delete_stuck_pods(app)


# ---- tier 2 responses ----------------------------------------------------


def fixed_provisioner(problem, app):
    problem.raise_provisioner_open_files(REPAIRED_OPEN_FILES)
    app.command("rollout", "status", "deployment/provisioner", "--timeout=300s", timeout=330)


def fleets_reference(problem, app):
    fixed_provisioner(problem, app)
    for group in app.capacity_groups:
        app.scaler_policy(group, **REPAIRED_FLEET_POLICY[group])
    delete_stuck_pods(app, app.capacity_groups)


def fleets_split_quota(problem, app):
    """An alternative valid repair: the push relay with no place to spare, and a different scale-in threshold."""
    fixed_provisioner(problem, app)
    app.scaler_policy("chat-gateway", max=17, scale_in={"metric": "saturation", "below": 25})
    app.scaler_policy("push-proxy", max=3, scale_in={"metric": "saturation", "below": 25})
    delete_stuck_pods(app, app.capacity_groups)


def fleets_policies_without_quota_split(problem, app):
    """Both fleets' scale-in fixed and stuck instances replaced, but the maximums still exceed the quota together."""
    fixed_provisioner(problem, app)
    for group in app.capacity_groups:
        app.scaler_policy(group, **REPAIRED_SCALE_IN)
    delete_stuck_pods(app, app.capacity_groups)


def fleets_raise_max_keeping_stuck(problem, app):
    fixed_provisioner(problem, app)
    app.scaler_policy("chat-gateway", max=2 * app.fleet_max, **REPAIRED_SCALE_IN)
    app.scaler_policy("push-proxy", max=2 * app.push_fleet_max, **REPAIRED_SCALE_IN)


def fleets_chat_only(problem, app):
    """Only the chat edge's policy repaired; the push relay still scales in on CPU. Graded on its data alone."""
    fixed_provisioner(problem, app)
    app.scaler_policy("chat-gateway", **REPAIRED_FLEET_POLICY["chat-gateway"])
    delete_stuck_pods(app, ("chat-gateway",))


def fleets_provisioner_only(problem, app):
    fixed_provisioner(problem, app)
    delete_stuck_pods(app, app.capacity_groups)


def fleets_automation_deleted(problem, app):
    fixed_provisioner(problem, app)
    delete_automation(app)
    delete_stuck_pods(app, app.capacity_groups)
    app.command("scale", "deployment/chat-gateway", "--replicas=16")
    app.command("scale", "deployment/push-proxy", "--replicas=4")


def fleets_bypass(problem, app):
    fleets_reference(problem, app)
    conf = app.command("get", "configmap", "chat-gateway-nginx", "-o", "jsonpath={.data.nginx\\.conf}")
    upstream = app.transit_url.removeprefix("http://")
    assert upstream in conf, upstream
    patch = {"data": {"nginx.conf": conf.replace(upstream, "mattermost:8065")}}
    app.command("patch", "configmap", "chat-gateway-nginx", "--type=merge", "-p", json.dumps(patch))
    # The backend admits only the provider network, so going around it means admitting the edge too.
    edge = {"podSelector": {"matchLabels": {"app": "chat-gateway"}}}
    policy = json.loads(app.command("get", "networkpolicy", "mattermost-ingress", "-o", "json"))
    policy["spec"]["ingress"][0]["from"].append(edge)
    app.command(
        "patch", "networkpolicy", "mattermost-ingress", "--type=merge", "-p", json.dumps({"spec": policy["spec"]})
    )
    app.command("rollout", "restart", "deployment/chat-gateway")
    app.command("rollout", "status", "deployment/chat-gateway", "--timeout=600s", timeout=630)


def fleets_chat_max14(problem, app):
    """Screen run 3 without its edge change: the baseline fleets_raise_edge_limit is compared with."""
    fixed_provisioner(problem, app)
    app.scaler_policy("chat-gateway", min=3, max=14, scale_in={"metric": "saturation", "below": 30})
    app.scaler_policy("push-proxy", min=2, max=4, scale_in={"metric": "saturation", "below": 30})
    delete_stuck_pods(app, app.capacity_groups)


def fleets_chat16_push4(problem, app):
    """Screen run 4, and the agents' usual split in screen5t: the reference's split with a different scale-in."""
    fixed_provisioner(problem, app)
    app.scaler_policy("chat-gateway", min=3, max=16, scale_in={"metric": "saturation", "below": 30})
    app.scaler_policy("push-proxy", min=2, max=4, scale_in={"metric": "saturation", "below": 30})
    delete_stuck_pods(app, app.capacity_groups)


def fleets_chat18_push2(problem, app):
    """The former reference: most of the quota for chat, too few relays to deliver over the impaired network."""
    fixed_provisioner(problem, app)
    app.scaler_policy("chat-gateway", **REPAIRED_SCALE_IN, max=18)
    app.scaler_policy("push-proxy", **REPAIRED_SCALE_IN, max=2)
    delete_stuck_pods(app, app.capacity_groups)


def fleets_split_keeping_cpu_scale_in(problem, app):
    """Screen 3 run 1 without its edge change: the quota split, but scale-in still on CPU."""
    fixed_provisioner(problem, app)
    app.scaler_policy("chat-gateway", max=16)
    app.scaler_policy("push-proxy", max=4)
    delete_stuck_pods(app, app.capacity_groups)


def fleets_raise_edge_limit(problem, app):
    """Screen run 3: fewer chat instances, each allowed four times the connections."""
    fixed_provisioner(problem, app)
    app.scaler_policy("chat-gateway", min=3, max=14, scale_in={"metric": "saturation", "below": 30})
    app.scaler_policy("push-proxy", min=2, max=4, scale_in={"metric": "saturation", "below": 30})
    conf = app.command("get", "configmap", "chat-gateway-nginx", "-o", "jsonpath={.data.nginx\\.conf}")
    limit = f"limit_conn edge {EDGE_CONNECTIONS};"
    assert limit in conf, limit
    patch = {"data": {"nginx.conf": conf.replace(limit, f"limit_conn edge {4 * EDGE_CONNECTIONS};")}}
    app.command("patch", "configmap", "chat-gateway-nginx", "--type=merge", "-p", json.dumps(patch))
    delete_stuck_pods(app, app.capacity_groups)
    app.command("rollout", "restart", "deployment/chat-gateway")
    app.command("rollout", "status", "deployment/chat-gateway", "--timeout=600s", timeout=630)


def fleets_push_disabled(problem, app):
    fleets_reference(problem, app)
    app.command("set", "env", "deployment/mattermost", "MM_EMAILSETTINGS_SENDPUSHNOTIFICATIONS=false")
    app.command("rollout", "status", "deployment/mattermost", "--timeout=600s", timeout=630)


#: name -> (response, expectation). An expectation is True (must pass), the set
#: of failure reasons the grade may report, or None (graded on its data alone).
CONTROLS = {
    "no_op": (no_op, DEGRADED_FAILURES | WITHOUT_AUTOMATION_FAILURES),
    "manual_scale_up": (manual_scale_up, DEGRADED_FAILURES | WITHOUT_AUTOMATION_FAILURES),
    "raise_max": (raise_max, DEGRADED_FAILURES | WITHOUT_AUTOMATION_FAILURES),
    "provisioner_only": (provisioner_only, DEGRADED_FAILURES | WITHOUT_AUTOMATION_FAILURES),
    "scale_in_only": (scale_in_only, DEGRADED_FAILURES | WITHOUT_AUTOMATION_FAILURES),
    "automation_deleted": (automation_deleted, WITHOUT_AUTOMATION_FAILURES),
    "bypass": (bypass, {"provider_network_bypassed"}),
    "restart_and_saturation_scale_in": (restart_and_saturation_scale_in, True),
    "reference": (reference, True),
}
TIER2_CONTROLS = {
    "no_op": (no_op, TIER2_FAILURES),
    "fleets_raise_max_keeping_stuck": (fleets_raise_max_keeping_stuck, None),
    "fleets_chat_only": (fleets_chat_only, None),
    "fleets_provisioner_only": (fleets_provisioner_only, TIER2_FAILURES),
    "fleets_automation_deleted": (
        fleets_automation_deleted,
        {
            "chat_not_served_while_degraded",
            "chat_not_served_after_instance_loss",
            "pushes_not_delivered_after_instance_loss",
        },
    ),
    "fleets_bypass": (fleets_bypass, {"provider_network_bypassed", "chat_not_served_while_degraded"}),
    "fleets_push_disabled": (fleets_push_disabled, {"push_delivery_suppressed"}),
    "fleets_policies_without_quota_split": (fleets_policies_without_quota_split, None),
    "fleets_split_quota": (fleets_split_quota, True),
    "fleets_chat_max14": (fleets_chat_max14, TIER2_FAILURES),
    "fleets_raise_edge_limit": (fleets_raise_edge_limit, TIER2_FAILURES),
    "fleets_chat16_push4": (fleets_chat16_push4, True),
    "fleets_chat18_push2": (fleets_chat18_push2, TIER2_FAILURES),
    "fleets_split_keeping_cpu_scale_in": (fleets_split_keeping_cpu_scale_in, TIER2_FAILURES),
    "reference": (fleets_reference, True),
}
TIER1B_CONTROLS = {**CONTROLS, "bypass": (bypass, DEGRADED_FAILURES | {"provider_network_bypassed"})}
TIER_CONTROLS = {"1": CONTROLS, "1b": TIER1B_CONTROLS, "2": TIER2_CONTROLS}

#: From inside a gateway pod: the backend directly, and the backend through the provider network.
PATH_PROBE = (
    "import sys,urllib.request\n"
    "try:\n"
    " urllib.request.urlopen(sys.argv[1],timeout=5).read();print('reachable')\n"
    "except Exception as exc:\n"
    " print('unreachable',type(exc).__name__)\n"
)


def direct_path(app):
    """The gateway can reach the backend only through the provider network."""
    pod = app.command("get", "pods", "-l", "app=chat-gateway", "-o", "jsonpath={.items[0].metadata.name}").strip()

    def probe(url):
        return app.command("exec", pod, "-c", "gateway", "--", "python", "-c", PATH_PROBE, url, timeout=60).strip()

    return {
        "direct": probe("http://mattermost:8065/api/v4/system/ping"),
        "through_provider": probe(f"{app.transit_url}/api/v4/system/ping"),
    }


def restore_healthy(app):
    """Undo whatever the last control or run changed: healthy traffic, shipped manifests, calibrated policy."""
    # Raising capacity is what repairs an impaired network; then it is put back.
    app.set_transit_capacity(app.transit_capacity_fixed)
    app.set_customer_rate(app.healthy_rate)
    app.apply(app.application_documents())
    # Apply keeps environment variables a control added imperatively (tier 1b's
    # open-files fix); the shipped provisioner has none.
    app.command("set", "env", "deployment/provisioner", "-c", "provisioner", "MAX_OPEN_FILES-")
    # A provisioner left out of descriptors by the last control starts clean,
    # and nginx rereads a configuration the last control may have rewritten.
    app.command("rollout", "restart", "deployment/provisioner")
    app.command("rollout", "restart", "deployment/chat-gateway")
    settle(app)
    if app.instance_quota:
        for group, replicas in zip(app.capacity_groups, (app.capacity_floor, PUSH_REPLICAS), strict=True):
            app.write_control(app.policy_file(group), calibrated_policy(app, group))
            app.command("scale", f"deployment/{group}", f"--replicas={replicas}")
    else:
        app.write_control("scaler.json", calibrated_policy(app))
        app.command("scale", "deployment/chat-gateway", f"--replicas={app.capacity_floor}")
    settle(app)
    app.wait_for_gateway()
    app.set_transit_capacity(app.transit_capacity)
    # Let counters and CPU samples cover healthy traffic only.
    time.sleep(30)


def restore_incident(problem, app):
    """Start each control from a cascade that formed on its own."""
    restore_healthy(app)
    problem.inject_fault()
    cascade = dict(problem.observed.get("cascade") or {})
    if app.instance_quota:
        cascade["compute"] = app.compute_stats()
        cascade["stuck"] = len(app.instances_provisioning())
        cascade["fleets"] = {group: app.fleet(group) for group in app.capacity_groups}
    return cascade


def visibility():
    """The agent's proxied kubeconfig must not reveal the provider namespace."""
    from sregym.conductor.conductor import Conductor, ConductorConfig

    conductor = Conductor(config=ConductorConfig())
    conductor.start_k8s_proxy()
    try:
        kubeconfig = str(conductor._agent_kubeconfig_path)

        def agent(*args):
            return subprocess.run(
                ["kubectl", "--kubeconfig", kubeconfig, *args], capture_output=True, text=True, timeout=60
            )

        namespaces = agent("get", "ns", "-o", "name").stdout.split()
        direct = agent("-n", PROVIDER_NAMESPACE, "get", "pods")
        everywhere = agent("get", "pods", "-A", "-o", "wide").stdout
        return {
            "listed": f"namespace/{PROVIDER_NAMESPACE}" in namespaces,
            "direct_get_refused": direct.returncode != 0,
            "in_all_namespaces": PROVIDER_NAMESPACE in everywhere,
        }
    finally:
        conductor.stop_k8s_proxy()


def validate(tier, only, check_visibility, deploy):
    problem = MattermostTransitSaturationProblem(tier)
    controls = TIER_CONTROLS[tier]
    app, oracle = problem.app, problem.mitigation_oracle
    result = {"application": "mattermost", "incident": "transit_saturation", "tier": tier, "controls": {}}
    started = time.time()
    stage = "start"

    def log(name, payload):
        print(json.dumps({"stage": name, "elapsed_seconds": round(time.time() - started, 2), **payload}), flush=True)

    try:
        stage = "deploy"
        if deploy:
            app.deploy()
        else:
            restore_healthy(app)
        pods = json.loads(app.command("get", "pods", "-o", "json"))["items"]
        result["images"] = sorted(
            {c.get("imageID") or c["image"] for p in pods for c in p.get("status", {}).get("containerStatuses", [])}
        )
        stage = "calibration"
        policy = json.loads(calibrated_policy(app))
        result["calibration"] = policy
        # A policy calibrated against healthy load must leave healthy load alone.
        assert policy["scale_in"]["below"] < policy["calibrated_healthy_cpu_percent"], policy

        stage = "healthy"
        oracle.capture_baseline()
        result["healthy"] = {"latency_ms": oracle.baseline_latency_ms, "fleet": app.gateway_fleet()}
        assert result["healthy"]["fleet"]["in_service"] >= app.capacity_floor, result["healthy"]
        log("healthy", result["healthy"])

        if tier == "1b":
            stage = "direct_path"
            result["direct_path"] = direct_path(app)
            assert result["direct_path"]["direct"].startswith("unreachable"), result["direct_path"]
            assert result["direct_path"]["through_provider"] == "reachable", result["direct_path"]

        if check_visibility:
            stage = "visibility"
            result["visibility"] = visibility()
            assert not result["visibility"]["listed"], result["visibility"]
            assert result["visibility"]["direct_get_refused"], result["visibility"]
            assert not result["visibility"]["in_all_namespaces"], result["visibility"]

        for name in only or controls:
            stage = name
            respond, expected = controls[name]
            entry = result["controls"][name] = {}
            entry["cascade"] = restore_incident(problem, app)
            respond(problem, app)
            grade = oracle.evaluate()
            entry["grade"] = grade
            entry["expected"] = "pass" if expected is True else "either" if expected is None else sorted(expected)
            if expected is None:
                entry["as_expected"] = True
            elif expected is True:
                entry["as_expected"] = bool(grade.get("success"))
            else:
                entry["as_expected"] = not grade.get("success") and grade.get("reason") in expected
            log(
                name,
                {"as_expected": entry["as_expected"], "reason": grade.get("reason"), "success": grade.get("success")},
            )
        result["passed"] = all(c["as_expected"] for c in result["controls"].values())
    except Exception as exc:
        result["passed"] = False
        result["error"] = f"{stage}: {type(exc).__name__}: {exc}"
        if stderr := getattr(exc, "stderr", None):
            result["stderr"] = str(stderr)[-4000:]
    result["elapsed_seconds"] = round(time.time() - started, 2)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=sorted(TIER_CONTROLS), default="1")
    parser.add_argument("--only", nargs="+", choices=sorted({*CONTROLS, *TIER1B_CONTROLS, *TIER2_CONTROLS}))
    parser.add_argument("--visibility", action="store_true")
    parser.add_argument("--skip-deploy", action="store_true", help="use the application already deployed")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    outcome = validate(args.tier, args.only, args.visibility, not args.skip_deploy)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(outcome, indent=2, default=str) + "\n")
    print(json.dumps({k: v for k, v in outcome.items() if k != "controls"}, indent=2, default=str))
    raise SystemExit(int(not outcome["passed"]))
