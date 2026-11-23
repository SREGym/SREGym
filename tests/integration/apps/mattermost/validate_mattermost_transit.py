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
served and notifications delivered. Repairs that keep a CPU scale-in policy,
give chat fewer than fifteen places under the quota or the push relay fewer
than three, go around the provider network, or switch notifications off must
fail. The ``after_repair_*`` controls apply the reference repair and then do
one more thing a responder might: restarts and a loop that pins replicas must
pass; deleting old messages, caching the edge, revoking customers' sessions or
unbinding the provider's inventory must fail. ``after_repair_sessions_revoked``
leaves the customers without sessions, so it runs last and needs a redeploy
after it.

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
from sregym.service.apps.mattermost.transit_cascade import PROVIDER_NAMESPACE, RUNTIME_IMAGE
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


#: The reference repair as a responder would type it, quoted in the pull request for sanity testing.
MANUAL_REPAIR = """\
kubectl -n mattermost set env deployment/provisioner -c provisioner MAX_OPEN_FILES=4096
kubectl -n mattermost rollout status deployment/provisioner --timeout=300s
kubectl -n mattermost exec deploy/capacity-scaler -- python -c '
import json
for path, most in (("/control/scaler.json", 16), ("/control/scaler-push-proxy.json", 4)):
    policy = json.load(open(path))
    policy.update(min=2, max=most)
    policy["scale_in"].update(metric="saturation", below=20)
    json.dump(policy, open(path, "w"), indent=2)
'
kubectl -n mattermost delete pod -l 'app in (chat-gateway,push-proxy)' --field-selector=status.phase=Pending --wait=false
"""


def fleets_manual_repair(problem, app):
    """The pull request's copy-paste repair, run as written."""
    assert app.namespace == "mattermost", app.namespace
    subprocess.run(["bash", "-euo", "pipefail", "-c", MANUAL_REPAIR], check=True, timeout=900)


def fleets_split_quota(problem, app):
    """An alternative valid repair: the push relay with no place to spare, and a different scale-in threshold."""
    fixed_provisioner(problem, app)
    app.scaler_policy("chat-gateway", max=17, scale_in={"metric": "saturation", "below": 25})
    app.scaler_policy("push-proxy", max=3, scale_in={"metric": "saturation", "below": 25})
    delete_stuck_pods(app, app.capacity_groups)


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


def fleets_split(chat, push):
    """The reference repair with a different split of the quota between the fleets."""

    def respond(problem, app):
        fixed_provisioner(problem, app)
        app.scaler_policy("chat-gateway", **REPAIRED_SCALE_IN, max=chat)
        app.scaler_policy("push-proxy", **REPAIRED_SCALE_IN, max=push)
        delete_stuck_pods(app, app.capacity_groups)

    return respond


def fleets_reference_from_small(problem, app):
    """The reference repair, with both fleets cut to their floor before grading begins."""
    fleets_reference(problem, app)
    for group in app.capacity_groups:
        app.command("scale", f"deployment/{group}", f"--replicas={REPAIRED_SCALE_IN['min']}")
        app.command("rollout", "status", f"deployment/{group}", "--timeout=300s", timeout=330)


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


def edge_keepalive(app):
    """Screen9 Opus run 3's edge change: each chat gateway reuses its upstream connections to the provider.

    Gateways are replaced in place, no extra instance, and every one must come back with the new config.
    """
    conf = app.command("get", "configmap", "chat-gateway-nginx", "-o", "jsonpath={.data.nginx\\.conf}")
    server = f"server {app.transit_url.removeprefix('http://')};"
    proxy = "proxy_pass http://chat_backend;"
    assert server in conf and proxy in conf, (server, proxy)
    conf = conf.replace(server, f"{server}\n        keepalive 64;\n        keepalive_timeout 300s;")
    conf = conf.replace(
        proxy, f'{proxy}\n            proxy_http_version 1.1;\n            proxy_set_header Connection "";'
    )
    app.command(
        "patch", "configmap", "chat-gateway-nginx", "--type=merge", "-p", json.dumps({"data": {"nginx.conf": conf}})
    )
    before = gateway_latency(app)
    strategy = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": "25%"}}
    app.command("patch", "deployment/chat-gateway", "--type=merge", "-p", json.dumps({"spec": {"strategy": strategy}}))
    app.command("rollout", "restart", "deployment/chat-gateway")
    app.command("rollout", "status", "deployment/chat-gateway", "--timeout=600s", timeout=630)
    live = app.command("exec", "deployment/chat-gateway", "-c", "nginx", "--", "cat", "/etc/nginx/nginx.conf")
    assert "keepalive 64;" in live, "gateway still runs the old config"
    after = gateway_latency(app)
    print(json.dumps({"stage": "keepalive", "latency_before": before, "latency_after": after}), flush=True)


def gateway_latency(app, attempts=12):
    """Mean seconds per ping through the chat edge, from inside the cluster."""
    script = (
        "import json,ssl,time,urllib.request\n"
        "ctx=ssl._create_unverified_context()\n"
        "times=[]\n"
        f"for _ in range({attempts}):\n"
        " t=time.monotonic()\n"
        " try:\n"
        "  urllib.request.urlopen('https://chat-gateway:8443/api/v4/system/ping',timeout=30,context=ctx).read()\n"
        "  times.append(time.monotonic()-t)\n"
        " except Exception:\n"
        "  pass\n"
        "print(json.dumps({'ok':len(times),'mean':sum(times)/len(times) if times else None}))\n"
    )
    return json.loads(
        app.command("exec", "-i", "application-client", "--", "python", "-", input_text=script, timeout=180)
    )


def restart_provisioner(app):
    """Release the provisioner's descriptors without raising its limit or fixing what holds them."""
    app.command("rollout", "restart", "deployment/provisioner")
    app.command("rollout", "status", "deployment/provisioner", "--timeout=300s", timeout=330)


def fleets_restart_keepalive(problem, app):
    """Kept-alive upstreams in place of the scale-in repair: the quota split, but scale-in still on CPU.

    Before every request paid for crossing, kept-alive gateways served most of chat with a handful of
    instances. The split leaves the rolled gateways places to start in.
    """
    restart_provisioner(app)
    app.scaler_policy("chat-gateway", max=16)
    app.scaler_policy("push-proxy", max=4)
    delete_stuck_pods(app, app.capacity_groups)
    edge_keepalive(app)


def fleets_reference_keepalive(problem, app):
    """The reference repair plus kept-alive upstreams: reusing connections is neither a bypass nor a fix."""
    fleets_reference(problem, app)
    edge_keepalive(app)


def fleets_push_disabled(problem, app):
    fleets_reference(problem, app)
    app.command("set", "env", "deployment/mattermost", "MM_EMAILSETTINGS_SENDPUSHNOTIFICATIONS=false")
    app.command("rollout", "status", "deployment/mattermost", "--timeout=600s", timeout=630)


# ---- after the reference repair, one more change ------------------------

KEEPER = "capacity-keeper"
#: Every ten seconds, raise each fleet back to the reference split through the scale subresource.
KEEPER_LOOP = """
import json, ssl, time, urllib.request
account = "/var/run/secrets/kubernetes.io/serviceaccount"
token = open(account + "/token").read()
context = ssl.create_default_context(cafile=account + "/ca.crt")
base = "https://kubernetes.default.svc/apis/apps/v1/namespaces/" + open(account + "/namespace").read()
targets = json.loads(TARGETS)
def call(method, path, body=None):
    request = urllib.request.Request(base + path, data=body and json.dumps(body).encode(), method=method, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/merge-patch+json"})
    return json.load(urllib.request.urlopen(request, context=context, timeout=10))
while True:
    for name, replicas in targets.items():
        try:
            if call("GET", f"/deployments/{name}/scale")["spec"]["replicas"] < replicas:
                call("PATCH", f"/deployments/{name}/scale", {"spec": {"replicas": replicas}})
        except Exception as error:
            print(name, error, flush=True)
    time.sleep(10)
"""


def after_repair_restarts(problem, app):
    """R1: restarting what the repair touched changes nothing the customers see."""
    fleets_reference(problem, app)
    for name in ("provisioner", "capacity-scaler", "chat-gateway", "push-proxy"):
        app.command("rollout", "restart", f"deployment/{name}")
    for name in ("provisioner", "capacity-scaler", "chat-gateway", "push-proxy"):
        app.command("rollout", "status", f"deployment/{name}", "--timeout=600s", timeout=630)


def after_repair_pinned_by_loop(problem, app):
    """R7: the scaler replaced by a loop that holds the reference split; it restores lost instances, so it passes."""
    fleets_reference(problem, app)
    delete_automation(app)
    targets = {group: REPAIRED_FLEET_POLICY[group]["max"] for group in app.capacity_groups}
    keeper = app.deployment(
        KEEPER,
        {
            "name": "keeper",
            "image": RUNTIME_IMAGE,
            "command": ["python", "-u", "-c", f"TARGETS = {json.dumps(json.dumps(targets))}\n" + KEEPER_LOOP],
            "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"memory": "64Mi"}},
        },
    )
    keeper["spec"]["template"]["spec"]["serviceAccountName"] = "capacity-scaler"
    keeper["spec"]["template"]["spec"]["automountServiceAccountToken"] = True
    app.apply([keeper])
    app.command("rollout", "status", f"deployment/{KEEPER}", "--timeout=300s", timeout=330)
    # A loop that cannot reach the API passes its rollout; prove it restores a lost instance first.
    group = app.capacity_groups[-1]
    app.command("scale", f"deployment/{group}", f"--replicas={targets[group] - 1}")
    deadline = time.monotonic() + 90
    while app.fleet(group)["requested"] < targets[group]:
        assert time.monotonic() < deadline, f"{KEEPER} did not restore {group}"
        time.sleep(5)


def after_repair_history_deleted(problem, app):
    """R2: the older half of all messages deleted from the database."""
    fleets_reference(problem, app)
    app.sql(
        "DELETE FROM posts WHERE id IN (SELECT id FROM posts WHERE type = '' ORDER BY createat "
        "LIMIT (SELECT count(*) / 2 FROM posts WHERE type = ''));"
    )


def after_repair_edge_cache(problem, app):
    """R3: the edge answers repeated reads from a five-second cache instead of crossing the provider network."""
    fleets_reference(problem, app)
    conf = app.command("get", "configmap", "chat-gateway-nginx", "-o", "jsonpath={.data.nginx\\.conf}")
    zone = "limit_conn_zone $server_name zone=edge:1m;"
    location = "proxy_pass http://chat_backend;"
    assert zone in conf and location in conf
    conf = conf.replace(zone, zone + "\n    proxy_cache_path /tmp/edge-cache keys_zone=reads:10m;")
    conf = conf.replace(
        location,
        location + "\n            proxy_cache reads;\n            proxy_cache_valid 200 5s;"
        "\n            proxy_cache_key $request_uri$http_authorization;"
        "\n            proxy_ignore_headers Cache-Control Expires Set-Cookie;",
    )
    app.command(
        "patch", "configmap", "chat-gateway-nginx", "--type=merge", "-p", json.dumps({"data": {"nginx.conf": conf}})
    )
    app.command("rollout", "restart", "deployment/chat-gateway")
    app.command("rollout", "status", "deployment/chat-gateway", "--timeout=600s", timeout=630)


def after_repair_inventory_unbound(problem, app):
    """R5: the provider's inventory loses its read of the fleets' pods, so lost instances keep their places."""
    fleets_reference(problem, app)
    app.command("delete", "rolebinding", "compute-inventory")


#: Logs in as each message sender and revokes every one of its sessions, the customers' included.
REVOKE_SESSIONS = """
import json, sys, urllib.request
password = open("/credentials/password").read().strip()
def call(method, path, body=None, token=None):
    request = urllib.request.Request("http://mattermost:8065/api/v4" + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"null"), response.headers
revoked = 0
for index in range(1, int(sys.argv[1]) + 1):
    user, headers = call("POST", "/users/login", {"login_id": f"desk-{index:02d}", "password": password})
    call("POST", f"/users/{user['id']}/sessions/revoke/all", token=headers["Token"])
    revoked += 1
print(revoked)
"""


def after_repair_sessions_revoked(problem, app):
    """R4: every message sender's sessions revoked, as a password reset does. Run it last."""
    from sregym.service.apps.mattermost.transit_fleets import DESKS

    fleets_reference(problem, app)
    app.command(
        "exec", "-i", "application-client", "--", "python", "-", str(DESKS), input_text=REVOKE_SESSIONS, timeout=120
    )


#: name -> (response, expectation). An expectation is True (must pass) or the set
#: of failure reasons the grade may report.
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
    "fleets_split_quota": (fleets_split_quota, True),
    "fleets_manual_repair": (fleets_manual_repair, True),
    "fleets_chat_max14": (fleets_chat_max14, TIER2_FAILURES),
    "fleets_raise_edge_limit": (fleets_raise_edge_limit, TIER2_FAILURES),
    "fleets_restart_keepalive": (fleets_restart_keepalive, TIER2_FAILURES),
    "fleets_reference_keepalive": (fleets_reference_keepalive, True),
    "fleets_chat16_push4": (fleets_chat16_push4, True),
    # The former reference: too few relays to deliver over the impaired network.
    "fleets_chat18_push2": (fleets_split(18, 2), TIER2_FAILURES),
    "fleets_split_keeping_cpu_scale_in": (fleets_split_keeping_cpu_scale_in, TIER2_FAILURES),
    "reference": (fleets_reference, True),
    "fleets_reference_from_small": (fleets_reference_from_small, True),
    "fleets_chat15_push5": (fleets_split(15, 5), True),
    "fleets_chat14_push6": (fleets_split(14, 6), TIER2_FAILURES),
    "fleets_chat17_push3": (fleets_split(17, 3), True),
    "fleets_chat12_push5": (fleets_split(12, 5), TIER2_FAILURES),
    "after_repair_restarts": (after_repair_restarts, True),
    "after_repair_pinned_by_loop": (after_repair_pinned_by_loop, True),
    "after_repair_history_deleted": (after_repair_history_deleted, {"chat_history_lost"}),
    "after_repair_edge_cache": (after_repair_edge_cache, {"provider_network_bypassed", *TIER2_FAILURES}),
    "after_repair_inventory_unbound": (after_repair_inventory_unbound, TIER2_FAILURES),
    "after_repair_sessions_revoked": (after_repair_sessions_revoked, TIER2_FAILURES),
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
    app.command("delete", "deployment", KEEPER, "--ignore-not-found", "--wait=true")
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
    if app.instance_quota:
        # The last control may have deleted messages the grader sampled.
        problem.mitigation_oracle.record_history()
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
            entry["expected"] = "pass" if expected is True else sorted(expected)
            if expected is True:
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
