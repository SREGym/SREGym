"""Mattermost behind a gateway whose backend is reached through a provider network.

Modelled on Slack's outage of 2021-01-04. The gateway reaches Mattermost through
a transit hop in a provider-managed namespace the responder cannot see or
change; the hop has finite capacity, and customer traffic arrives from outside
the cluster at a rate the service does not control. New gateway instances must
register with a provisioner before serving, and the provisioner's own backend
calls cross the same hop. Capacity automation runs two independent policies: a
CPU-keyed scale-in and a saturation-keyed scale-out.

Nothing is scripted after the surge starts. Waiting connections block gateway
workers, so CPU falls while saturation climbs; scale-out requests instances that
cannot finish provisioning, because each registration holds descriptors for as
long as the slow hop takes and the provisioner runs out; and the stuck instances
fill the policy's maximum.
"""

import ast
import json
import subprocess
import time

from sregym.service.apps.components.kubernetes import KubernetesCommands
from sregym.service.apps.mattermost import in_house
from sregym.service.apps.mattermost.application import MattermostApplication
from sregym.service.apps.mattermost.cascade import CONTROL_VOLUME, MattermostCascadeApplication
from sregym.service.apps.mattermost.in_house import InHouseImage

#: Hidden from the agent by sregym.service.agent_visibility_policy.
PROVIDER_NAMESPACE = "provider-network"
RUNTIME_IMAGE = "python:3.12.13-alpine3.23"
TRANSIT_PORT = 8065

GATEWAY_WORKERS = 8
CAPACITY_FLOOR = 3
FLEET_MAX = 20
SCALE_OUT_STEP = 4
#: The provisioner's shipped open-files limit, set by its start command.
PROVISIONER_OPEN_FILES = 32
#: Configuration documents a registration fetches across the provider network.
PROVISIONER_FETCHES = 6
REGISTRATION_TIMEOUT = 3

#: Customer arrivals per second in normal operation, and during the surge.
HEALTHY_RATE = 60
SURGE_RATE = 140
#: Transit connections per second, as provisioned, and after the provider's fix.
TRANSIT_CAPACITY = 80
TRANSIT_CAPACITY_FIXED = 1000
TRANSIT_IMPAIRED_LATENCY = 1.2
TRANSIT_IMPAIRED_LOSS = 0.1

#: Tier 2. Instances the provider lets the account run at once.
INSTANCE_QUOTA = 20

TRANSIT_DESCRIPTION = (
    " Customer traffic reaches chat through a `chat-gateway` fronting service with a bounded"
    " worker pool. The gateway reaches the chat backend through a cloud provider's managed"
    " network, which is not under your administration. New gateway instances register with a"
    " `provisioner` before serving, and a `capacity-scaler` adjusts the gateway's replica"
    " count automatically."
)
#: Tier 1b: every component the responder can see, described alike.
CATALOG_DESCRIPTION = (
    " Services in this deployment: `chat-gateway`, the in-house front end customer chat traffic enters"
    " through, which forwards it to Mattermost; `mattermost`, the chat backend, with its PostgreSQL cluster;"
    " `provisioner`, the in-house service new instances register with before they take traffic; and"
    " `capacity-scaler`, the in-house autoscaler that sets `chat-gateway`'s replica count. The backend is"
    " reached through a cloud provider's managed network, which is not under your administration. Metrics,"
    " logs and traces are collected by the cluster's observability stack."
)
#: A business requirement, stated as the operators' own; it says nothing about the fault.
CAPACITY_REQUIREMENT = " The service requires at least {floor} `chat-gateway` instances in service at all times."


def shipped(source):
    """Source as it is shipped in a pod spec: no comments, no docstrings.

    The program travels inside the Deployment's command, so anyone who can read
    the Deployment can read it. Its behaviour is the system's; the authors' notes
    about why it behaves that way are not. Every string standing alone as a
    statement goes, wherever it is: a composed program carries an extension's
    docstring in its middle, not first.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(node, field, None)
            if isinstance(statements, list) and any(is_bare_string(s) for s in statements):
                kept = [s for s in statements if not is_bare_string(s)]
                setattr(node, field, kept or ([ast.Pass()] if field == "body" else []))
    return ast.unparse(tree) + "\n"


def is_bare_string(statement):
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def defined_names(statement):
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {statement.name}
    if isinstance(statement, (ast.Assign, ast.AnnAssign)):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        return {t.id for t in targets if isinstance(t, ast.Name)}
    return set()


def is_entry_point(statement):
    return isinstance(statement, ast.If) and "__main__" in ast.unparse(statement.test)


def composed_source(base, extension):
    """One program: ``base`` with every definition ``extension`` replaces removed.

    The extension's definitions take the removed ones' places in the shared
    globals, so base's remaining functions call them; base's own entry point is
    dropped. Nothing replaced survives as dead code to be read in the pod spec.
    """
    replacing = ast.parse(extension)
    replaced = set().union(*(defined_names(s) for s in replacing.body))
    kept = [s for s in ast.parse(base).body if not (defined_names(s) & replaced) and not is_entry_point(s)]
    return shipped(ast.unparse(ast.Module(body=kept + replacing.body, type_ignores=[])))


def still_provisioning(pods):
    """Names of pods whose init step (registration) has not finished."""
    return [
        p["metadata"]["name"]
        for p in pods
        if any(not s.get("ready") for s in p.get("status", {}).get("initContainerStatuses", []))
    ]


class ProviderNetwork(KubernetesCommands):
    """kubectl against the provider namespace, which the agent's proxy hides."""

    namespace = PROVIDER_NAMESPACE


class MattermostTransitApplication(MattermostCascadeApplication):
    cascade_deployments = ("chat-gateway", "capacity-scaler", "provisioner")

    capacity_floor = CAPACITY_FLOOR
    gateway_workers = GATEWAY_WORKERS
    fleet_max = FLEET_MAX
    scale_out_step = SCALE_OUT_STEP
    provisioner_open_files = PROVISIONER_OPEN_FILES
    provisioner_fetches = PROVISIONER_FETCHES
    registration_timeout = REGISTRATION_TIMEOUT
    healthy_rate = HEALTHY_RATE
    surge_rate = SURGE_RATE
    transit_capacity = TRANSIT_CAPACITY
    transit_capacity_fixed = TRANSIT_CAPACITY_FIXED
    transit_impaired_latency = TRANSIT_IMPAIRED_LATENCY
    transit_impaired_loss = TRANSIT_IMPAIRED_LOSS
    #: No instance quota, and a stated capacity requirement.
    instance_quota = None
    #: Deployments the capacity scaler sizes.
    capacity_groups = ("chat-gateway",)
    #: Port the chat gateway's Service accepts customers on.
    gateway_port = 8080
    states_capacity_requirement = True

    def __init__(self, tier="single", storage_class="openebs-hostpath"):
        super().__init__(tier, storage_class)
        self.provider = ProviderNetwork()

    def get_app_json(self):
        result = MattermostApplication.get_app_json(self)
        result["Desc"] += TRANSIT_DESCRIPTION
        if self.states_capacity_requirement:
            result["Desc"] += CAPACITY_REQUIREMENT.format(floor=self.capacity_floor)
        return result

    def runtime_source(self, name):
        return shipped(super().runtime_source(name))

    @property
    def transit_url(self):
        return f"http://transit.{PROVIDER_NAMESPACE}.svc:{TRANSIT_PORT}"

    # ---- documents -------------------------------------------------------

    def provider_documents(self):
        state = [{"name": "state", "emptyDir": {}}]
        transit = self.deployment(
            "transit",
            {
                "name": "transit",
                "image": RUNTIME_IMAGE,
                "command": ["python", "-u", "-c", self.runtime_source("transit.py")],
                "env": [
                    {"name": "TRANSIT_ROUTES", "value": f"{TRANSIT_PORT}=mattermost.{self.namespace}.svc:8065"},
                    {"name": "TRANSIT_CAPACITY", "value": str(self.transit_capacity)},
                    {"name": "TRANSIT_IMPAIRED_LATENCY", "value": str(self.transit_impaired_latency)},
                    {"name": "TRANSIT_IMPAIRED_LOSS", "value": str(self.transit_impaired_loss)},
                ],
                "volumeMounts": [{"name": "state", "mountPath": "/run/transit"}],
                "resources": {"requests": {"cpu": "200m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            state,
        )
        customers = self.deployment(
            "customers",
            {
                "name": "customers",
                "image": RUNTIME_IMAGE,
                "command": ["python", "-u", "-c", self.runtime_source("customers.py")],
                "env": [
                    {
                        "name": "CUSTOMERS_TARGET",
                        "value": f"http://chat-gateway.{self.namespace}.svc:8080/api/v4/system/ping",
                    },
                    # Silent until deployment has finished and calibration starts.
                    {"name": "CUSTOMERS_RATE", "value": "0"},
                ],
                "volumeMounts": [{"name": "state", "mountPath": "/run/customers"}],
                "resources": {"requests": {"cpu": "200m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
            },
            state,
        )
        service = self.service("transit", TRANSIT_PORT)
        return [transit, service, customers]

    def registration_source(self):
        """The gateway's init step: register, retrying until the provisioner accepts."""
        return (
            "import json,os,time,urllib.request\n"
            "body=json.dumps({'instance':os.environ['HOSTNAME']}).encode()\n"
            "attempt=0\n"
            "while True:\n"
            " attempt+=1\n"
            " try:\n"
            "  request=urllib.request.Request('http://provisioner:8080/register',data=body,method='POST',"
            "headers={'Content-Type':'application/json'})\n"
            f"  with urllib.request.urlopen(request,timeout={self.registration_timeout}) as response:\n"
            "   if response.status==200:\n"
            "    print(f'registered after {attempt} attempts',flush=True);break\n"
            " except Exception as exc:\n"
            "  print(f'registration attempt {attempt} failed: {exc}',flush=True)\n"
            " time.sleep(1)\n"
        )

    def application_documents(self):
        documents = MattermostApplication.application_documents(self)
        control = [{"name": "control", "persistentVolumeClaim": {"claimName": CONTROL_VOLUME}}]

        gateway = self.deployment(
            "chat-gateway",
            {
                "name": "gateway",
                "image": RUNTIME_IMAGE,
                "command": ["python", "-u", "-c", self.runtime_source("edge_gateway.py")],
                "env": [
                    {"name": "GATEWAY_UPSTREAM", "value": self.transit_url},
                    {"name": "GATEWAY_WORKERS", "value": str(self.gateway_workers)},
                ],
                "volumeMounts": [{"name": "control", "mountPath": "/control"}],
                "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 5},
                "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            control,
            initContainers=[
                {
                    "name": "register",
                    "image": RUNTIME_IMAGE,
                    "command": ["python", "-u", "-c", self.registration_source()],
                    "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "64Mi"}},
                }
            ],
        )
        gateway["spec"]["replicas"] = self.capacity_floor
        gateway["spec"]["strategy"] = {"type": "RollingUpdate"}

        provisioner = self.deployment(
            "provisioner",
            {
                "name": "provisioner",
                "image": RUNTIME_IMAGE,
                # $0 names the process, $1 is the program.
                "command": [
                    "sh",
                    "-c",
                    f'ulimit -n {self.provisioner_open_files} && exec python -u -c "$1"',
                    "provisioner",
                    self.runtime_source("provisioner.py"),
                ],
                "env": [
                    {"name": "PROVISIONER_BACKEND", "value": f"{self.transit_url}/api/v4/system/ping"},
                    {"name": "PROVISIONER_FETCHES", "value": str(self.provisioner_fetches)},
                ],
                "volumeMounts": [{"name": "data", "mountPath": "/var/lib/provisioner"}],
                "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            [{"name": "data", "emptyDir": {}}],
        )

        scaler = self.deployment(
            "capacity-scaler",
            {
                "name": "scaler",
                "image": RUNTIME_IMAGE,
                "command": [
                    "python",
                    "-u",
                    "-c",
                    composed_source(
                        super().runtime_source("capacity_scaler.py"), super().runtime_source("fleet_scaler.py")
                    ),
                ],
                "env": [
                    {"name": "NAMESPACE", "value": self.namespace},
                    {"name": "SCALER_TARGET", "value": "chat-gateway"},
                    {"name": "SCALER_INTERVAL", "value": str(self.scaler_interval)},
                ],
                "volumeMounts": [{"name": "control", "mountPath": "/control"}],
                "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 5},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            control,
        )
        scaler["spec"]["template"]["spec"]["serviceAccountName"] = "capacity-scaler"
        scaler["spec"]["template"]["spec"]["automountServiceAccountToken"] = True

        return [
            *documents,
            *self.scaler_rbac(),
            self.service("chat-gateway", 8080),
            self.service("capacity-scaler", 8080),
            self.service("provisioner", 8080),
            gateway,
            provisioner,
            scaler,
        ]

    # ---- lifecycle -------------------------------------------------------

    def deploy(self):
        if not self.provider_exists():
            subprocess.run(["kubectl", "create", "namespace", PROVIDER_NAMESPACE], check=True, capture_output=True)
        self.provider.apply(self.provider_documents())
        for name in ("transit", "customers"):
            self.provider.command("rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=330)
        MattermostApplication.deploy(self)
        for name in self.cascade_deployments:
            self.command("rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=330)
        self.wait_for_gateway()
        self.set_customer_rate(self.healthy_rate)
        if not self.command("get", "configmap", "capacity-calibrated", "--ignore-not-found", "-o", "name").strip():
            # Let the gateway's CPU sampler see a few windows of real traffic.
            time.sleep(20)
            self.calibrate_scaler()
            self.apply(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "capacity-calibrated"},
                        "data": {"policy": self.read_control("scaler.json")},
                    }
                ]
            )

    def provider_exists(self):
        found = subprocess.run(
            ["kubectl", "get", "namespace", PROVIDER_NAMESPACE, "--ignore-not-found", "-o", "name"],
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(found.stdout.strip())

    def cleanup(self):
        subprocess.run(
            [
                "kubectl",
                "delete",
                "namespace",
                PROVIDER_NAMESPACE,
                "--ignore-not-found",
                "--wait=true",
                "--timeout=180s",
            ],
            capture_output=True,
            check=True,
            timeout=200,
        )
        super().cleanup()

    # ---- capacity policy -------------------------------------------------

    def default_policy(self):
        return {
            "enabled": True,
            "min": 1,
            "max": self.fleet_max,
            "scale_in": {"enabled": True, "metric": "cpu", "below": 25},
            "scale_out": {
                "enabled": True,
                "metric": "saturation",
                "above": 75,
                "step": self.scale_out_step,
                "cooldown_seconds": 60,
            },
        }

    def current_policy(self):
        try:
            return json.loads(self.read_control("scaler.json"))
        except Exception:
            return self.default_policy()

    def scaler_policy(self, **changes):
        """Change the policy in force (the shipped one before calibration); nested sections merge."""
        policy = self.current_policy()
        for key, value in changes.items():
            policy[key] = {**policy[key], **value} if isinstance(value, dict) and key in policy else value
        self.write_control("scaler.json", json.dumps(policy, indent=2) + "\n")
        return policy

    def calibrate_scaler(self):
        """Scale-in threshold from measured healthy CPU, as an operator would set it.

        Healthy load sits well inside the band; a gateway whose workers are all
        waiting on the network falls far below it.
        """
        healthy = self.healthy_cpu()
        policy = self.scaler_policy(
            scale_in={"below": max(3, round(healthy * 0.6))},
            calibrated_healthy_cpu_percent=healthy,
        )
        self.write_control(
            "capacity-policy.txt",
            "Capacity automation calibrated against observed healthy load.\n"
            f"healthy gateway CPU: {healthy}%\n"
            f"scale in: toward {policy['scale_in']['below']}% CPU, removing replicas in proportion "
            "while CPU is below it\n"
            f"scale out: {policy['scale_out']['step']} replicas when worker saturation is above "
            f"{policy['scale_out']['above']}%\n"
            f"replica bounds enforced by policy: {policy['min']}-{policy['max']}\n"
            + (f"service capacity floor: {self.capacity_floor}\n" if self.states_capacity_requirement else ""),
        )
        return policy

    # ---- provider and customers (harness only) ---------------------------

    def provider_read(self, deployment, path):
        return json.loads(self.provider.command("exec", f"deployment/{deployment}", "--", "cat", path))

    def provider_write(self, deployment, path, value):
        self.provider.command(
            "exec", "-i", f"deployment/{deployment}", "--", "sh", "-c", 'cat > "$1"', "write", path, input_text=value
        )

    def set_transit_capacity(self, per_second):
        self.provider_write("transit", "/run/transit/capacity", f"{float(per_second)}\n")

    def transit_stats(self):
        return self.provider_read("transit", "/run/transit/stats.json")

    def set_customer_rate(self, per_second):
        self.provider_write("customers", "/run/customers/rate", f"{float(per_second)}\n")

    def customer_stats(self):
        return self.provider_read("customers", "/run/customers/stats.json")

    # ---- observation -----------------------------------------------------

    def gateway_fleet(self):
        """Requested replicas, pods in service, and pods still provisioning."""
        deployment = json.loads(self.command("get", "deployment", "chat-gateway", "-o", "json"))
        pods = json.loads(self.command("get", "pods", "-l", "app=chat-gateway", "-o", "json"))["items"]
        live = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        provisioning = still_provisioning(live)
        in_service = [
            p
            for p in live
            if p.get("status", {}).get("phase") == "Running"
            and any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))
        ]
        return {
            "requested": deployment["spec"].get("replicas", 0),
            "in_service": len(in_service),
            "provisioning": len(provisioning),
        }

    def instances_provisioning(self):
        """Pods of every scaled fleet whose registration has not finished."""
        pods = json.loads(
            self.command("get", "pods", "-l", f"app in ({','.join(self.capacity_groups)})", "-o", "json")
        )["items"]
        return still_provisioning([p for p in pods if not p["metadata"].get("deletionTimestamp")])

    def provisioner_errors(self, since_seconds=120):
        """Open-files failures in the provisioner's own log, as a responder reads it."""
        try:
            log = self.command("logs", "deployment/provisioner", f"--since={int(since_seconds)}s", timeout=60)
        except subprocess.CalledProcessError:
            return None
        return sum("Too many open files" in line for line in log.splitlines())

    def start_workload(self):
        """A no-op: customers are part of the environment and always on."""
        return

    def raise_open_files(self, limit):
        """The responder's fix of the provisioner's open-files limit, as its start command sets it."""
        command = f'ulimit -n {int(limit)} && exec python -u -c "$1"'
        patch = [{"op": "replace", "path": "/spec/template/spec/containers/0/command/2", "value": command}]
        self.command("patch", "deployment/provisioner", "--type=json", "-p", json.dumps(patch))


class MattermostTransitPackagedApplication(MattermostTransitApplication):
    """Tier 1b: tier 1 with the shortcuts a real deployment does not offer.

    In-house programs ship in images, so a Deployment no longer shows their
    code; the backend accepts connections only from the provider network, so
    nothing in the application namespace can time the backend around it; the
    gateway publishes its own counters but no latency summary, and the scaler
    records activities rather than its readings; and the description lists
    every component alike.
    """

    def get_app_json(self):
        result = MattermostApplication.get_app_json(self)
        result["Desc"] += CATALOG_DESCRIPTION
        if self.states_capacity_requirement:
            result["Desc"] += CAPACITY_REQUIREMENT.format(floor=self.capacity_floor)
        return result

    def provisioner_source(self):
        base = MattermostCascadeApplication.runtime_source(self, "provisioner.py")
        return composed_source(base, MattermostCascadeApplication.runtime_source(self, "provisioner_service.py"))

    def in_house_images(self):
        raw = super(MattermostTransitApplication, self).runtime_source
        gateway = InHouseImage(
            "chat/chat-gateway",
            {
                "gateway.py": composed_source(raw("edge_gateway.py"), raw("gateway_metrics.py")),
                "register.py": self.registration_source(),
            },
        )
        provisioner = InHouseImage(
            "platform/provisioner",
            {
                "provisioner.py": self.provisioner_source(),
                "start": "#!/bin/sh\n"
                f'ulimit -n "${{MAX_OPEN_FILES:-{self.provisioner_open_files}}}"\n'
                "exec python -u /srv/provisioner.py\n",
            },
            executables=("start",),
        )
        scaler = InHouseImage(
            "platform/capacity-scaler",
            {
                "scaler.py": composed_source(
                    composed_source(raw("capacity_scaler.py"), raw("fleet_scaler.py")), raw("activity_history.py")
                )
            },
        )
        return [gateway, provisioner, scaler]

    def render(self):
        documents = super().render()
        for document in documents:
            if document["kind"] == "Pod" and document["metadata"]["name"] == "application-client":
                document["metadata"].setdefault("labels", {})["app"] = "application-client"
        return documents

    def backend_ingress(self):
        """Mattermost accepts connections from the provider network and from inside its own network only."""
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "mattermost-ingress"},
            "spec": {
                "podSelector": {"matchLabels": {"app": self.slug}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": PROVIDER_NAMESPACE}}},
                            {"podSelector": {"matchLabels": {"app": "application-client"}}},
                        ],
                        "ports": [{"protocol": "TCP", "port": 8065}],
                    }
                ],
            },
        }

    def application_documents(self):
        documents = super().application_documents()
        gateway, provisioner, scaler = (image.reference for image in self.in_house_images())
        for document in documents:
            name = document["metadata"]["name"]
            if document["kind"] != "Deployment" or name not in self.cascade_deployments:
                continue
            spec = document["spec"]["template"]["spec"]
            container = spec["containers"][0]
            container["imagePullPolicy"] = "Never"
            if name == "chat-gateway":
                container["image"], container["command"] = gateway, ["python", "-u", "/srv/gateway.py"]
                register = spec["initContainers"][0]
                register.update(image=gateway, imagePullPolicy="Never", command=["python", "-u", "/srv/register.py"])
            elif name == "provisioner":
                container["image"], container["command"] = provisioner, ["/srv/start"]
            else:
                container["image"], container["command"] = scaler, ["python", "-u", "/srv/scaler.py"]
        return [*documents, self.backend_ingress()]

    def deploy(self):
        absent = in_house.missing(self.in_house_images(), in_house.node_images())
        if absent:
            raise RuntimeError(
                f"In-house images are not on the node: {absent}. Build them on each node with "
                "`uv run python -m sregym.service.apps.mattermost.in_house --tier 1b`."
            )
        super().deploy()

    def raise_open_files(self, limit):
        self.command("set", "env", "deployment/provisioner", "-c", "provisioner", f"MAX_OPEN_FILES={int(limit)}")


TRANSIT_TIERS = {"1": MattermostTransitApplication, "1b": MattermostTransitPackagedApplication}


def transit_application(tier="1", **options):
    if tier == "2":
        # Imported here: the two-fleet tier builds on this module.
        from sregym.service.apps.mattermost.transit_fleets import MattermostTransitFleetsApplication

        return MattermostTransitFleetsApplication(**options)
    if tier not in TRANSIT_TIERS:
        raise ValueError(f"Unsupported transit tier {tier!r}: choose {(*TRANSIT_TIERS, '2')}")
    return TRANSIT_TIERS[tier](**options)


# Existing class names remain available to callers.
MattermostTransit = MattermostTransitApplication
