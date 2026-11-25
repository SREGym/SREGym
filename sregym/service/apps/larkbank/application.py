"""Larkbank, a bank whose customers' payees and standing orders live in a Cassandra ring.

After Monzo's incident of 29 July 2019
(monzo.com/blog/2019/09/08/why-monzo-wasnt-working-on-july-29th).
Everything the agent sees is in the larkbank namespace:

    api           the customer app's API: payees, standing orders, payments made
    scheduler     pays every standing order when it is due, over the payment rail
    ring          Cassandra, a StatefulSet: keyspace bank, three copies of every
                  row, read and written at QUORUM. Three nodes to begin with
    ring-config   a ConfigMap: the settings added to cassandra.yaml, the token
                  planned for each node, and the script that starts a node
    payment-rail  a Service with no selector: the payment rail's API

Hidden in khaos: the payment rail itself, the customers (who set up again what
they can no longer see), and the record of what each did
(docker/apps/larkbank/cmd/world).
"""

import base64
import json
import os
import random
import secrets
import subprocess
import time
from pathlib import Path

import yaml

from sregym.service.apps.base import Application
from sregym.service.apps.components.kubernetes import KubernetesCommands
from sregym.service.apps.helpers import image_reference
from sregym.service.kubectl import KubeCtl

HERE = Path(__file__).parent
CASSANDRA_IMAGE = "cassandra:4.1.12"
HIDDEN_NAMESPACE = "khaos"
RING = "ring"
RING_CONFIG = "ring-config"
API = "api"
SCHEDULER = "scheduler"
RAIL = "payment-rail"
WORLD = "larkbank-world"
WORLD_STATUS = "http://127.0.0.1:8091"
PROBE = "ring-probe"
#: Run in the test node: wait for the Cassandra JVM, print its arguments, then the cassandra.yaml it reads.
PROBE_READ = """\
pid=""
daemon="Cassandra""Daemon"  # split, so that this script's own command line does not match
for i in $(seq 90); do
  for p in /proc/[0-9]*; do
    if grep -q "$daemon" "$p/cmdline" 2>/dev/null; then pid=${p#/proc/}; break 2; fi
  done
  sleep 1
done
[ -n "$pid" ] || exit 0
tr '\\0' '\\n' < /proc/$pid/cmdline
echo ===CONFIG===
config=$(tr '\\0' '\\n' < /proc/$pid/cmdline | sed -n 's/^-Dcassandra.config=//p' | tail -1)
config=${config#file://}
cat "${config:-/etc/cassandra/cassandra.yaml}"
"""
CHANGE_CAUSE = "CAP-311: three more nodes ahead of month end"

#: Seconds between a standing order's payments (the customers' "month").
EVERY_SECONDS = 150
#: The ring's nodes to begin with, and with the three the on-call adds.
NODES = 3
NODES_WANTED = 6

#: Where each node sits on the ring, as a fraction of it. The first three are the ring to begin with (three
#: copies of every row, so each holds everything). The other three are the planned positions of the nodes to
#: add: with them in, a fifth of the ring has all three of its copies on new nodes, and almost a third has two.
RING_PLAN = (0.30, 0.40, 0.55, 0.75, 0.90, 0.95)

OVERRIDES = """\
# Larkbank's settings, added to the image's cassandra.yaml when a node starts.

# OPS-1184: a node that has lost its disk is restored from last night's backup,
# not rebuilt from its neighbours.
auto_bootstrap: false
"""

START = """\
#!/bin/sh
# Starts a ring node. Larkbank's settings (overrides.yaml) and this node's
# token (the line of tokens for its ordinal) are added to cassandra.yaml.
set -e
ordinal=${HOSTNAME##*-}
token=$(sed -n "$((ordinal + 1))p" /etc/ring/tokens)
if [ -z "$token" ]; then
  echo "no token is planned for node $ordinal" >&2
  exit 1
fi
cat /etc/ring/overrides.yaml >> /etc/cassandra/cassandra.yaml
echo "initial_token: $token" >> /etc/cassandra/cassandra.yaml
exec docker-entrypoint.sh cassandra -f
"""

TIERS = {
    "small": {
        "customers": 240,
        # What a customer does on finding something they set up gone from the app: set it up again at once,
        # or look again a while later and then set it up again. The rest wait for the bank to fix it.
        "hasty": 0.3,
        "patient": 0.4,
        # Customers who add a new payee and standing order: so many in the first minutes after the nodes are
        # added (what the ring is asked to keep while it is wrong), the rest over the half hour after. And
        # customers who cancel a standing order at some point in that half hour.
        "adders_early": 40,
        "adders": 30,
        "cancellers": 24,
        # Seconds of normal work before the nodes are added, and from all of them being in to the handover.
        "steady_seconds": 40,
        "wild_seconds": 150,
    },
}

FIRST_NAMES = ("Asha", "Ben", "Chidi", "Dana", "Elif", "Farid", "Grace", "Hugo", "Ines", "Jonas", "Kira", "Liam")
LAST_NAMES = ("Okafor", "Patel", "Quinn", "Rossi", "Sato", "Taylor", "Usman", "Vega", "Walsh", "Young", "Zhang")
PAYEES = (
    ("Landlord", "RENT"), ("Mum", "HOUSEKEEPING"), ("Council Tax", "CTAX"), ("Gym", "MEMBERSHIP"),
    ("Savings", "SAVE"), ("Nursery", "FEES"), ("Flatmate", "BILLS"), ("Piano Lessons", "LESSONS"),
    ("Charity", "DONATION"), ("Car Finance", "CAR"),
)  # fmt: skip


def image():
    """Larkbank's image. SREGYM_LARKBANK_IMAGE overrides it while developing."""
    return os.environ.get("SREGYM_LARKBANK_IMAGE") or image_reference("larkbank")


def hidden_image():
    return os.environ.get("SREGYM_LARKBANK_HIDDEN_IMAGE") or image_reference("larkbank-hidden")


def tokens(plan=RING_PLAN):
    """The plan's positions as Murmur3 tokens, one for each node."""
    return [int(-(2**63) + fraction * 2**64) for fraction in plan]


def text(value):
    return "'" + str(value).replace("'", "''") + "'"


def world_spec(tier, seed=20190729):
    """The customers: what each has set up, and (known to the world only) what each will do."""
    rng = random.Random(seed)

    def account():
        return {
            "sort_code": "-".join(f"{rng.randrange(100):02d}" for _ in range(3)),
            "account_number": f"{rng.randrange(10**8):08d}",
        }

    customers = []
    for n in range(tier["customers"]):
        kinds = rng.sample(PAYEES, rng.choice((1, 2, 2, 3)))
        payees = [{"name": name, **account()} for name, _ in kinds]
        orders = [
            {
                "payee": k,
                "amount": rng.randrange(20, 900) * 100,
                "reference": f"{reference} {rng.randrange(1000, 9999)}",
            }
            for k, (_, reference) in enumerate(kinds)
            if k == 0 or rng.random() < 0.4
        ]
        share = rng.random()
        customers.append(
            {
                "id": f"C{1001 + n}",
                "name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                "joined_days_ago": rng.randrange(40, 1500),
                "period": round(rng.uniform(10, 16), 2),
                "payees": payees,
                "orders": orders,
                "redo": "hasty"
                if share < tier["hasty"]
                else "patient"
                if share < tier["hasty"] + tier["patient"]
                else "",
                "add_at": -1,
                "add": None,
                "add_payee": None,
                "cancel_at": -1,
                "cancel": 0,
            }
        )
    pool = list(range(len(customers)))
    rng.shuffle(pool)
    for k in range(tier["adders_early"] + tier["adders"]):
        customer = customers[pool.pop()]
        early = k < tier["adders_early"]
        customer["add_at"] = round(rng.uniform(40, 150) if early else rng.uniform(150, 1500), 1)
        customer["add_payee"] = {
            "name": rng.choice(("Window Cleaner", "Dog Walker", "Allotment", "Choir")),
            **account(),
        }
        customer["add"] = {
            "payee": 0,
            "amount": rng.randrange(10, 200) * 100,
            "reference": f"NEW {rng.randrange(1000, 9999)}",
        }
    # A customer who cancels is one who waits for the bank when something is gone, so nobody cancels
    # something they have set up twice.
    waits = [i for i in pool if not customers[i]["redo"]]
    for i in waits[: tier["cancellers"]]:
        customers[i]["cancel_at"] = round(rng.uniform(30, 1500), 1)
        customers[i]["cancel"] = rng.randrange(len(customers[i]["orders"]))
    return customers


class LarkbankApplication(KubernetesCommands, Application):
    app_id = "larkbank"
    metadata_file = HERE / "metadata.json"

    def __init__(self, scale_tier="small"):
        super().__init__(self.metadata_file)
        self.load_app_json()
        self.helm_deploy = False
        self.tier = dict(TIERS[scale_tier])
        self.spec = world_spec(self.tier)
        self.kubectl = KubeCtl()
        self.app_name = self.name
        self.description = self.get_app_json()["Desc"]

    def fqdn(self, name):
        return f"{name}.{self.namespace}.svc.cluster.local"

    def handover_description(self, added):
        """What the agent is told: the service, and the note of the engineer it takes over from."""
        note = (
            f" You are taking over from the on-call engineer, who left this note: 'At {added} UTC I added three "
            f"nodes to the ring for month end (CAP-311: kubectl scale statefulset {RING} --replicas={NODES_WANTED}, "
            f"as the runbook says). They came up fine and nodetool status shows all six up and normal. A few "
            f"minutes later support started hearing from customers whose payees and standing orders have vanished "
            f"from the app, and some standing orders were not paid. Others see everything. I do not know why; "
            f"nothing in the ring looks wrong to me. What we owe customers: everything each of them had set up "
            f"is there again, exactly as they left it; whatever they have done in the app since stays done; and "
            f"every standing order is paid when it is due, once. The app has to stay up while you work: we cannot "
            f"take {API} or the ring away from customers. We still need the three extra nodes for month end, so "
            f"they have to be in the ring and carrying their share before you hand back, and adding a node must "
            f"never do this again. Over to you.'"
        )
        return self.get_app_json()["Desc"] + note

    # ---- manifests -------------------------------------------------------

    def ring_config_documents(self, overrides=OVERRIDES):
        return [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": RING_CONFIG},
                "data": {
                    "overrides.yaml": overrides,
                    "tokens": "".join(f"{token}\n" for token in tokens()),
                    "start.sh": START,
                },
            }
        ]

    def ring_documents(self, replicas=NODES, annotations=None):
        labels = {"app": RING}
        container = {
            "name": "cassandra",
            "image": CASSANDRA_IMAGE,
            "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "/etc/ring/start.sh"],
            "env": [
                {"name": "CASSANDRA_SEEDS", "value": f"{RING}-0.{self.fqdn(RING)}"},
                {"name": "CASSANDRA_CLUSTER_NAME", "value": "larkbank"},
                {"name": "CASSANDRA_NUM_TOKENS", "value": "1"},
                {"name": "CASSANDRA_ENDPOINT_SNITCH", "value": "GossipingPropertyFileSnitch"},
                {"name": "CASSANDRA_DC", "value": "dc1"},
                {"name": "MAX_HEAP_SIZE", "value": "320M"},
                {"name": "HEAP_NEWSIZE", "value": "80M"},
                {
                    "name": "JVM_EXTRA_OPTS",
                    "value": "-Dcassandra.ring_delay_ms=10000 -Dcassandra.skip_wait_for_gossip_to_settle=0",
                },
                {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
            ],
            "ports": [{"containerPort": 9042, "name": "cql"}, {"containerPort": 7000, "name": "intra"}],
            # Ready once the node is up and normal in the ring.
            "readinessProbe": {
                "exec": {"command": ["sh", "-c", 'nodetool status | grep -E "^UN +$POD_IP "']},
                "initialDelaySeconds": 15,
                "periodSeconds": 5,
                "timeoutSeconds": 10,
            },
            "resources": {"requests": {"cpu": "200m", "memory": "700Mi"}, "limits": {"memory": "1400Mi"}},
            "volumeMounts": [
                {"name": "data", "mountPath": "/var/lib/cassandra"},
                {"name": "config", "mountPath": "/etc/ring"},
            ],
        }
        return [
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": RING},
                "spec": {
                    "clusterIP": "None",
                    "publishNotReadyAddresses": True,
                    "selector": labels,
                    "ports": [{"port": 9042, "name": "cql"}],
                },
            },
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "metadata": {"name": RING, "labels": labels, "annotations": annotations or {}},
                "spec": {
                    "serviceName": RING,
                    # Pods are started together, not one after another: a scale-up to six starts three nodes at once.
                    "podManagementPolicy": "Parallel",
                    "replicas": replicas,
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "terminationGracePeriodSeconds": 60,
                            "containers": [container],
                            "volumes": [{"name": "config", "configMap": {"name": RING_CONFIG}}],
                        },
                    },
                    "volumeClaimTemplates": [
                        {
                            "metadata": {"name": "data"},
                            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}},
                        }
                    ],
                },
            },
        ]

    def service_documents(self, name, command, env, port=True):
        labels = {"app": name}
        container = {
            "name": name,
            "image": image(),
            "imagePullPolicy": "IfNotPresent",
            "command": [command],
            "env": [{"name": "RING", "value": f"{self.fqdn(RING)}:9042"}, *env],
            "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "512Mi"}},
        }
        if port:
            container["ports"] = [{"containerPort": 8080}]
            container["readinessProbe"] = {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 2}
            # Leave the Service before stopping, so a restart drops no request.
            container["lifecycle"] = {"preStop": {"exec": {"command": ["sleep", "4"]}}}
        documents = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "labels": labels},
                "spec": {
                    "replicas": 2 if port else 1,
                    "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}
                    if port
                    else {"type": "Recreate"},
                    "selector": {"matchLabels": labels},
                    "template": {"metadata": {"labels": labels}, "spec": {"containers": [container]}},
                },
            }
        ]
        if port:
            documents.append(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": name},
                    "spec": {"selector": labels, "ports": [{"port": 8080, "targetPort": 8080}]},
                }
            )
        return documents

    def api_documents(self):
        return self.service_documents(
            API, "/usr/local/bin/api", [{"name": "EVERY_SECONDS", "value": str(EVERY_SECONDS)}]
        )

    def scheduler_documents(self):
        env = [
            {"name": "RAIL_URL", "value": f"http://{RAIL}:8080"},
            {"name": "RAIL_KEY", "valueFrom": {"secretKeyRef": {"name": RAIL, "key": "key"}}},
        ]
        return self.service_documents(SCHEDULER, "/usr/local/bin/scheduler", env, port=False)

    def rail_link_documents(self, ip):
        """The payment rail as Larkbank reaches it: an outside endpoint, by address."""
        return [
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": RAIL},
                "spec": {"ports": [{"port": 8080, "targetPort": 8080}]},
            },
            {
                "apiVersion": "v1",
                "kind": "Endpoints",
                "metadata": {"name": RAIL},
                "subsets": [{"addresses": [{"ip": ip}], "ports": [{"port": 8080}]}],
            },
        ]

    def world_documents(self, key):
        labels = {"app": WORLD}
        hidden = ("id", "period", "payees", "orders", "redo", "add_at", "add", "add_payee", "cancel_at", "cancel")
        spec = [{k: customer[k] for k in hidden} for customer in self.spec]
        container = {
            "name": "world",
            "image": hidden_image(),
            "imagePullPolicy": "IfNotPresent",
            "command": ["/usr/local/bin/world"],
            "args": [f"--api=http://{self.fqdn(API)}:8080"],
            "env": [{"name": "RAIL_KEY", "value": key}],
            "ports": [{"containerPort": 8080}],
            "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 2},
            "resources": {"requests": {"cpu": "150m", "memory": "64Mi"}, "limits": {"memory": "1Gi"}},
            "volumeMounts": [{"name": "config", "mountPath": "/config"}],
        }
        return [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": WORLD, "namespace": HIDDEN_NAMESPACE},
                "data": {"world.json": json.dumps(spec)},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": WORLD, "namespace": HIDDEN_NAMESPACE, "labels": labels},
                "spec": {
                    "replicas": 1,
                    "strategy": {"type": "Recreate"},
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "terminationGracePeriodSeconds": 5,
                            "containers": [container],
                            "volumes": [{"name": "config", "configMap": {"name": WORLD}}],
                        },
                    },
                },
            },
        ]

    def seed_cql(self, now=None):
        """The schema, and everything customers had set up before today, written long ago."""
        now = time.time() if now is None else now
        rng = random.Random(4)
        lines = [(HERE / "schema.cql").read_text(), "CONSISTENCY ALL;"]

        def stamp(seconds):
            return int(seconds * 1000)

        for n, customer in enumerate(self.spec):
            joined = now - customer["joined_days_ago"] * 86400
            lines.append(
                f"INSERT INTO bank.customer (customer_id, name, joined) VALUES ({text(customer['id'])}, "
                f"{text(customer['name'])}, {stamp(joined)}) USING TIMESTAMP {stamp(joined) * 1000};"
            )
            ids = []
            for k, payee in enumerate(customer["payees"]):
                made = joined + rng.uniform(0.01, 0.5) * (now - joined)
                ids.append(f"pe_{n:04x}{k:02x}{rng.getrandbits(40):010x}")
                lines.append(
                    "INSERT INTO bank.payee (customer_id, payee_id, name, sort_code, account_number, created_at) VALUES "
                    f"({text(customer['id'])}, {text(ids[-1])}, {text(payee['name'])}, {text(payee['sort_code'])}, "
                    f"{text(payee['account_number'])}, {stamp(made)}) USING TIMESTAMP {stamp(made) * 1000};"
                )
            for k, order in enumerate(customer["orders"]):
                made = joined + rng.uniform(0.5, 0.9) * (now - joined)
                # Due every period since; the next time falls somewhere in the period that starts now.
                first_due = now - rng.uniform(0, EVERY_SECONDS)
                lines.append(
                    "INSERT INTO bank.standing_order (customer_id, order_id, payee_id, amount, reference, every_seconds, "
                    f"first_due, created_at) VALUES ({text(customer['id'])}, 'so_{n:04x}{k:02x}{rng.getrandbits(40):010x}', "
                    f"{text(ids[order['payee']])}, {order['amount']}, {text(order['reference'])}, {EVERY_SECONDS}, "
                    f"{stamp(first_due)}, {stamp(made)}) USING TIMESTAMP {stamp(made) * 1000};"
                )
        return "\n".join(lines) + "\n"

    def deploy(self):
        self.ensure_namespace(self.namespace)
        self.ensure_namespace(HIDDEN_NAMESPACE)
        self.description = self.get_app_json()["Desc"]
        if not self.command("get", "secret", RAIL, "--ignore-not-found", "-o", "name").strip():
            self.command("create", "secret", "generic", RAIL, f"--from-literal=key=rk_live_{secrets.token_hex(16)}")
        key = self.secret(RAIL, "key")
        self.apply(self.ring_config_documents())
        self.apply(self.ring_documents())
        self.wait_ring(NODES, 600)
        self.seed()
        for node in range(NODES):
            self.nodetool(node, "flush")
        self.world_gone()
        self.hidden("apply", "-f", "-", input_text=yaml.safe_dump_all(self.world_documents(key), sort_keys=False))
        self.hidden("rollout", "status", f"deployment/{WORLD}", "--timeout=300s", timeout=320)
        ip = self.hidden("get", "pods", "-l", f"app={WORLD}", "-o", "jsonpath={.items[0].status.podIP}").strip()
        self.apply(self.rail_link_documents(ip))
        self.apply(self.api_documents())
        self.apply(self.scheduler_documents())
        for name in (API, SCHEDULER):
            self.command("rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=320)
        self.wait_for(lambda: "now" in self.world_get("/clock"), 120, 2, "the customers")
        self.world_post("/phase?name=live")

    def seed(self, tries=3):
        """Write the schema and what customers had set up. The same rows each time, so a try that did not
        finish (a node still settling just after the ring formed) is simply made again."""
        statements, last = self.seed_cql(), ""
        for _ in range(tries):
            try:
                last = self.cql(statements, timeout=180)
            except subprocess.TimeoutExpired:
                last = "Timeout: cqlsh did not finish"
            if not any(word in last for word in ("Error", "Exception", "Unavailable", "Timeout", "Invalid")):
                return
            time.sleep(15)
        raise RuntimeError(f"the ring was not seeded: {last[-600:]}")

    def world_gone(self):
        """Wait until no world from an earlier deployment is left: its customers would use this one."""
        subprocess.run(
            ["kubectl", "-n", HIDDEN_NAMESPACE, "wait", "--for=delete", "pod", "-l", f"app={WORLD}", "--timeout=120s"],
            capture_output=True,
            timeout=180,
        )

    def cleanup(self):
        for kind in ("deployment", "configmap"):
            subprocess.run(
                ["kubectl", "-n", HIDDEN_NAMESPACE, "delete", kind, WORLD, "--ignore-not-found", "--timeout=120s"],
                capture_output=True,
                timeout=180,
            )
        self.world_gone()
        subprocess.run(
            ["kubectl", "delete", "namespace", self.namespace, "--ignore-not-found", "--wait=false"],
            capture_output=True,
            timeout=120,
        )
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            out = subprocess.run(
                ["kubectl", "get", "namespace", self.namespace, "--ignore-not-found", "-o", "name"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if not out.stdout.strip():
                return
            time.sleep(3)

    # ---- helpers ---------------------------------------------------------

    def ensure_namespace(self, name):
        out = subprocess.run(
            ["kubectl", "get", "namespace", name, "--ignore-not-found", "-o", "name"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if not out.stdout.strip():
            subprocess.run(["kubectl", "create", "namespace", name], capture_output=True, timeout=60, check=True)

    def hidden(self, *args, input_text=None, timeout=60):
        return subprocess.run(
            ["kubectl", "-n", HIDDEN_NAMESPACE, *args],
            input=input_text,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        ).stdout

    def wait_for(self, predicate, timeout, interval=5, what="condition"):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except (subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
                last = error
            time.sleep(interval)
        raise TimeoutError(f"timed out after {timeout}s waiting for {what}" + (f": {last}" if last else ""))

    def secret(self, name, key):
        encoded = self.command("get", "secret", name, "-o", f"jsonpath={{.data.{key}}}").strip()
        return base64.b64decode(encoded).decode()

    def cql(self, statements, node=0, timeout=300):
        """Run CQL on a ring node. Returns cqlsh's output, errors included."""
        done = subprocess.run(
            ["kubectl", "-n", self.namespace, "exec", "-i", f"{RING}-{node}", "--", "cqlsh", "--request-timeout=60"],
            input=statements,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return done.stdout + done.stderr

    def nodetool(self, node, *args, timeout=600):
        return self.command("exec", f"{RING}-{node}", "--", "nodetool", *args, timeout=timeout)

    def ring_status(self, node=0):
        """The ring as a node sees it: [{'state': 'UN', 'address': ..., 'host_id': ...}]."""
        nodes = []
        for line in self.nodetool(node, "status", timeout=60).splitlines():
            fields = line.split()
            if len(fields) >= 7 and len(fields[0]) == 2 and fields[0][0] in "UD" and fields[0][1] in "NLJM":
                nodes.append({"state": fields[0], "address": fields[1], "host_id": fields[-2]})
        return nodes

    def nodes_normal(self):
        """How many nodes the ring has up and normal, or -1 if it cannot be asked."""
        try:
            return sum(node["state"] == "UN" for node in self.ring_status())
        except (subprocess.SubprocessError, ValueError):
            return -1

    def wait_ring(self, count, timeout=600):
        """Until the StatefulSet has that many pods ready and the ring that many nodes, all up and normal."""

        def settled():
            ready = self.command("get", "statefulset", RING, "-o", "jsonpath={.status.readyReplicas}").strip()
            status = self.ring_status()
            return ready == str(count) and len(status) == count and all(node["state"] == "UN" for node in status)

        self.wait_for(settled, timeout, 3, f"a ring of {count} nodes, up and normal")

    def api(self, method, path, body=None, timeout=60):
        """A request to the customer API from one of its pods. Returns (status, decoded body or None)."""
        args = [
            "exec",
            f"deploy/{API}",
            "--",
            "curl",
            "-s",
            "-m",
            "30",
            "-o",
            "/dev/stderr",
            "-w",
            "%{http_code}",
            "-X",
            method,
        ]
        if body is not None:
            args += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
        done = subprocess.run(
            ["kubectl", "-n", self.namespace, *args, f"http://127.0.0.1:8080{path}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        try:
            return int(done.stdout.strip() or 0), json.loads(done.stderr)
        except ValueError:
            return int(done.stdout.strip() or 0), None

    # ---- the world (hidden) ------------------------------------------------

    def world_get(self, path, timeout=300):
        out = self.hidden(
            "exec", f"deploy/{WORLD}", "--", "wget", "-qO-", "-T", "200", f"{WORLD_STATUS}{path}", timeout=timeout
        )
        return json.loads(out)

    def world_post(self, path, timeout=300):
        out = self.hidden(
            "exec", f"deploy/{WORLD}", "--", "wget", "-qO-", "-T", "200", "--post-data=", f"{WORLD_STATUS}{path}",
            timeout=timeout,
        )  # fmt: skip
        return json.loads(out)

    def world_clock(self):
        return self.world_get("/clock")["now"]

    def truth(self):
        """What customers set up, saw and did, and what the payment rail took."""
        return self.world_get("/truth")

    def final(self):
        """What every customer's app shows now."""
        return self.world_get("/final")

    def dedupe(self, keep="oldest", payees=True, timeout=900):
        """Remove what customers set up a second time, from the hidden side. Returns what it did."""
        out = self.hidden(
            "exec", f"deploy/{WORLD}", "--", "/usr/local/bin/repair",
            f"-api=http://{self.fqdn(API)}:8080",
            f"-ring={self.fqdn(RING)}:9042",
            f"-keep={keep}",
            f"-payees={'true' if payees else 'false'}",
            timeout=timeout,
        )  # fmt: skip
        return json.loads(out.strip().splitlines()[-1])

    # ---- the incident, and what puts it right ----------------------------------

    def scale_ring(self, replicas, cause=None):
        self.command("scale", "statefulset", RING, f"--replicas={replicas}")
        if cause:
            self.command("annotate", "statefulset", RING, f"kubernetes.io/change-cause={cause}", "--overwrite")

    def remove_node(self, node):
        """Take a node out in good order: it hands what it holds to the nodes that take over its part of the
        ring, then its pod and its disk go."""
        self.nodetool(node, "decommission", timeout=900)
        self.scale_ring(node)
        self.command("wait", "--for=delete", f"pod/{RING}-{node}", "--timeout=180s", timeout=200)
        self.command("delete", "pvc", f"data-{RING}-{node}", "--wait=true", timeout=200)

    def remove_new_nodes(self, between=None):
        for node in range(self.nodes_running() - 1, NODES - 1, -1):
            self.remove_node(node)
            if between:
                between()
        self.wait_ring(NODES, 300)

    def nodes_running(self):
        return int(self.command("get", "statefulset", RING, "-o", "jsonpath={.spec.replicas}").strip())

    def kill_new_nodes(self):
        """The quick way out: the new pods and their disks deleted, and the ring told to forget them."""
        gone = [pod for pod in range(NODES, self.nodes_running())]
        self.scale_ring(NODES)
        for node in gone:
            self.command("delete", "pod", f"{RING}-{node}", "--force", "--grace-period=0", "--ignore-not-found")
            self.command("delete", "pvc", f"data-{RING}-{node}", "--wait=false", "--ignore-not-found")
        self.wait_for(lambda: all(n["state"][0] == "D" for n in self.ring_status()[NODES:]) or True, 60, 5)
        time.sleep(25)
        for node in self.ring_status():
            if node["state"][0] == "D":
                self.nodetool(0, "removenode", node["host_id"], timeout=600)
        self.wait_ring(NODES, 300)

    def repair_in_place(self, cleanup=True):
        """Keep the new nodes: repair every node, then clean up the old ones."""
        for node in range(self.nodes_running()):
            self.nodetool(node, "repair", "--full", "bank", timeout=900)
        if cleanup:
            for node in range(NODES):
                self.nodetool(node, "cleanup", "bank", timeout=900)

    def fix_setting(self):
        """New nodes take their share of the data from the ring before they serve."""
        self.apply(self.ring_config_documents(OVERRIDES.replace("auto_bootstrap: false", "auto_bootstrap: true")))
        # A mounted ConfigMap reaches a running pod after a while; new pods read it when they start.
        time.sleep(2)

    def add_nodes(self, count=NODES_WANTED, timeout=600):
        """One at a time: Cassandra refuses a second node while one is still taking its data."""
        for nodes in range(self.nodes_running() + 1, count + 1):
            self.scale_ring(nodes)
            self.wait_ring(nodes, timeout)

    def setting_in_force(self):
        """The auto_bootstrap setting a node added now would start with.

        A node is started from the StatefulSet's pod template as it is now, cut off from the ring, and asked
        what it got: whatever the template mounts, runs, sets in the environment or passes to the JVM.
        """
        self.remove_probe()
        try:
            self.apply(self.probe_documents())
            self.command("wait", "--for=condition=Ready", f"pod/{PROBE}", "--timeout=120s", timeout=140)
            container = self.probe_container
            seen = self.command("exec", PROBE, "-c", container, "--", "sh", "-c", PROBE_READ, timeout=140)
        finally:
            self.remove_probe()
        if "===CONFIG===" not in seen:
            raise ValueError("the test node did not start Cassandra")
        args, _, config = seen.partition("===CONFIG===")
        return {"planned": effective_auto_bootstrap(args.splitlines(), config)}

    def remove_probe(self):
        self.command("delete", "pod,networkpolicy", PROBE, "--ignore-not-found", "--grace-period=0", timeout=120)

    def probe_documents(self):
        """A pod like the next ring node (the template's containers, volumes and ordinal), with no network and
        its own cluster name, so that whatever it starts cannot join the ring."""
        ring = json.loads(self.command("get", "statefulset", RING, "-o", "json"))
        spec = ring["spec"]["template"]["spec"]
        claims = [claim["metadata"]["name"] for claim in ring["spec"].get("volumeClaimTemplates", [])]
        spec["volumes"] = [volume for volume in spec.get("volumes", []) if volume["name"] not in claims]
        spec["volumes"] += [{"name": name, "emptyDir": {}} for name in claims]
        spec["hostname"] = f"{RING}-{NODES}"
        spec["restartPolicy"] = "Never"
        spec.pop("subdomain", None)
        for container in spec["containers"]:
            for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
                container.pop(probe, None)
            env = [
                var
                for var in container.get("env", [])
                if var["name"] not in ("CASSANDRA_SEEDS", "CASSANDRA_CLUSTER_NAME")
            ]
            container["env"] = env + [
                {"name": "CASSANDRA_SEEDS", "value": "127.0.0.1"},
                {"name": "CASSANDRA_CLUSTER_NAME", "value": "larkbank-probe"},
            ]
        self.probe_container = spec["containers"][0]["name"]
        labels = {"larkbank-probe": "true"}
        return [
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": PROBE},
                "spec": {"podSelector": {"matchLabels": labels}, "policyTypes": ["Ingress", "Egress"]},
            },
            {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": PROBE, "labels": labels}, "spec": spec},
        ]

    def new_nodes_started_empty(self):
        """The added nodes that started with auto_bootstrap off, that is, joined without taking their data."""
        empty = []
        for node in range(NODES, self.nodes_running()):
            text = self.command("exec", f"{RING}-{node}", "--", "cat", "/etc/cassandra/cassandra.yaml")
            if not parse_auto_bootstrap(text):
                empty.append(node)
        return empty

    def load_old_data(self, nodes=range(NODES)):
        """Put the rows the old nodes still hold back into the ring as it is now: a snapshot of each old node
        loaded with sstableloader, which writes every row to the nodes that are its replicas today."""
        hosts = ",".join(f"{RING}-{n}.{self.fqdn(RING)}" for n in range(2))
        script = (
            "set -e; nodetool snapshot -t reload bank >/dev/null; rm -rf /tmp/load; "
            "for d in /var/lib/cassandra/data/bank/*-*/; do t=$(basename $d); t=${t%-*}; "
            "mkdir -p /tmp/load/bank/$t; cp $d/snapshots/reload/* /tmp/load/bank/$t/; done; "
            "for t in customer payee standing_order payment; do "
            f'MAX_HEAP_SIZE=200M JVM_OPTS="-Xmx200m" sstableloader -d {hosts} /tmp/load/bank/$t >/dev/null 2>&1; done'
        )
        for node in nodes:
            self.command("exec", f"{RING}-{node}", "--", "sh", "-c", script, timeout=900)

    def pause_scheduler(self, paused=True):
        self.command("scale", "deployment", SCHEDULER, f"--replicas={0 if paused else 1}")
        if not paused:
            self.command("rollout", "status", f"deployment/{SCHEDULER}", "--timeout=120s", timeout=140)


def effective_auto_bootstrap(jvm_args, config):
    """What Cassandra makes of auto_bootstrap given its JVM arguments and its cassandra.yaml: the file, then
    -Dcassandra.settings.auto_bootstrap, then -Dcassandra.auto_bootstrap (which Cassandra reads last)."""
    value = parse_auto_bootstrap(config)
    for name in ("-Dcassandra.settings.auto_bootstrap=", "-Dcassandra.auto_bootstrap="):
        for arg in jvm_args:
            if arg.strip().startswith(name):
                value = arg.strip()[len(name) :].lower() == "true"
    return value


def parse_auto_bootstrap(yaml_text):
    """What a cassandra.yaml (or part of one) sets auto_bootstrap to; True if it does not say."""
    value = True
    for line in yaml_text.splitlines():
        key, _, rest = line.partition(":")
        if key.strip() == "auto_bootstrap" and not line.lstrip().startswith("#"):
            value = rest.split("#")[0].strip().lower() not in ("false", "no", "off")
    return value
