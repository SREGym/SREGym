"""CoreDNS saturation caused by ndots:5 DNS query amplification.

A stress workload (analytics-collector) generates high-volume DNS lookups whose
names contain fewer dots than the Kubernetes default ndots:5.  This triggers
search-path expansion, multiplying every query into several suffixed lookups and
overwhelming the resource-constrained, single-replica CoreDNS pod.  The result
is cluster-wide DNS latency and timeouts.

Canonical fix: set ``dnsConfig.options: [{name: ndots, value: "2"}]`` on the
offending deployment so search-path amplification stops.
"""

import json

import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.coredns_saturation_mitigation import CoreDNSMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

_COREDNS_DEPLOYMENT = "coredns"
_COREDNS_NAMESPACE = "kube-system"
_STRESS_DEPLOYMENT = "analytics-collector"

# Annotation key used to persist the original CoreDNS configuration on the
# deployment itself.  This survives process crashes and avoids fragile temp
# files or hardcoded defaults.
_ORIGINAL_CONFIG_ANNOTATION = "original-config"


class CoreDNSSaturationNdots(Problem):
    """Inject CoreDNS saturation via ndots:5 query amplification."""

    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()

        self.root_cause = self.build_structured_root_cause(
            component="deployment/coredns",
            namespace="kube-system",
            description=(
                "CoreDNS is saturated due to DNS query amplification caused by Kubernetes default ndots:5 configuration. "
                "A service generating high-volume external DNS requests causes excessive search path lookups, "
                "overwhelming the resource-constrained CoreDNS pods and causing cluster-wide name resolution latency and timeouts."
            ),
        )

        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = CoreDNSMitigationOracle(problem=self)

        self.app.create_workload()

    # ------------------------------------------------------------------
    # Fault lifecycle
    # ------------------------------------------------------------------
    @mark_fault_injected
    def inject_fault(self):
        print("== CoreDNS Saturation Fault Injection ==")

        # 1. Snapshot original CoreDNS config into an annotation on the
        #    deployment.  If the annotation already exists (re-injection after
        #    a crash), skip to avoid overwriting the true original.
        self._save_coredns_original()

        # 2. Constrain CoreDNS: 1 replica, tight resource limits.
        patch_body = {
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": _COREDNS_DEPLOYMENT,
                                "resources": {
                                    "limits": {"cpu": "150m", "memory": "128Mi"},
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                },
                            }
                        ]
                    }
                },
            }
        }
        try:
            self.apps_v1.patch_namespaced_deployment(_COREDNS_DEPLOYMENT, _COREDNS_NAMESPACE, patch_body)
            print("Patched CoreDNS deployment limits and replicas.")
            self.kubectl.exec_command(
                f"kubectl rollout restart deployment {_COREDNS_DEPLOYMENT} -n {_COREDNS_NAMESPACE}"
            )
            self.kubectl.exec_command(
                f"kubectl rollout status deployment {_COREDNS_DEPLOYMENT} -n {_COREDNS_NAMESPACE} --timeout=60s"
            )
        except Exception as e:
            print(f"Error patching CoreDNS: {e}")

        # 3. Deploy DNS stress workload in the app namespace.
        stress_deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": _STRESS_DEPLOYMENT,
                "namespace": self.namespace,
                "labels": {"app": _STRESS_DEPLOYMENT},
            },
            "spec": {
                "replicas": 2,
                "selector": {"matchLabels": {"app": _STRESS_DEPLOYMENT}},
                "template": {
                    "metadata": {"labels": {"app": _STRESS_DEPLOYMENT}},
                    "spec": {
                        "containers": [
                            {
                                "name": "collector",
                                "image": "python:3.12-slim",
                                "command": [
                                    "python",
                                    "-c",
                                    "import socket, struct, random, threading, time\n"
                                    "\n"
                                    "def parse_resolv_conf():\n"
                                    "    nameserver = '10.96.0.10'\n"
                                    "    search_paths = []\n"
                                    "    ndots = 5\n"
                                    "    try:\n"
                                    "        with open('/etc/resolv.conf', 'r') as f:\n"
                                    "            for line in f:\n"
                                    "                parts = line.strip().split()\n"
                                    "                if not parts or parts[0].startswith('#'):\n"
                                    "                    continue\n"
                                    "                if parts[0] == 'nameserver':\n"
                                    "                    nameserver = parts[1]\n"
                                    "                elif parts[0] == 'search':\n"
                                    "                    search_paths = parts[1:]\n"
                                    "                elif parts[0] == 'options':\n"
                                    "                    for opt in parts[1:]:\n"
                                    "                        if opt.startswith('ndots:'):\n"
                                    "                            ndots = int(opt.split(':')[1])\n"
                                    "    except Exception:\n"
                                    "        pass\n"
                                    "    return nameserver, search_paths, ndots\n"
                                    "\n"
                                    "def make_dns_query(domain, qtype=1):\n"
                                    "    txid = random.randint(0, 65535)\n"
                                    "    header = struct.pack('>HHHHHH', txid, 0x0100, 1, 0, 0, 0)\n"
                                    "    parts = [p for p in domain.split('.') if p]\n"
                                    "    qname = b''.join(bytes([len(p)]) + p.encode() for p in parts)\n"
                                    "    qname += b'\\x00'\n"
                                    "    qname += struct.pack('>HH', qtype, 1)\n"
                                    "    return header + qname\n"
                                    "\n"
                                    "def flood(nameserver, search_paths, ndots):\n"
                                    "    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
                                    "    while True:\n"
                                    "        domain = f'analytics-sync-{random.randint(100000,999999)}.cluster.local'\n"
                                    "        dot_count = domain.count('.')\n"
                                    "        if dot_count < ndots:\n"
                                    "            for suffix in search_paths:\n"
                                    "                full_domain = f'{domain}.{suffix}'\n"
                                    "                try:\n"
                                    "                    sock.sendto(make_dns_query(full_domain, 1), (nameserver, 53))\n"
                                    "                    sock.sendto(make_dns_query(full_domain, 28), (nameserver, 53))\n"
                                    "                except Exception: pass\n"
                                    "        try:\n"
                                    "            sock.sendto(make_dns_query(domain, 1), (nameserver, 53))\n"
                                    "            sock.sendto(make_dns_query(domain, 28), (nameserver, 53))\n"
                                    "        except Exception: pass\n"
                                    "        time.sleep(0.06)\n"
                                    "\n"
                                    "nameserver, search_paths, ndots = parse_resolv_conf()\n"
                                    "threads = [threading.Thread(target=flood, args=(nameserver, search_paths, ndots), daemon=True) for _ in range(10)]\n"
                                    "for t in threads: t.start()\n"
                                    "for t in threads: t.join()\n",
                                ],
                                "resources": {
                                    "limits": {"cpu": "100m", "memory": "64Mi"},
                                    "requests": {"cpu": "50m", "memory": "32Mi"},
                                },
                            }
                        ]
                    },
                },
            },
        }

        try:
            stress_yaml = yaml.dump(stress_deployment)
            self.kubectl.exec_command(f"kubectl apply -f - -n {self.namespace}", input_data=stress_yaml)
            print("Deployed DNS stress deployment (analytics-collector).")
        except Exception as e:
            print(f"Error deploying stress workload: {e}")

    @mark_fault_injected
    def recover_fault(self):
        print("== CoreDNS Saturation Fault Recovery ==")

        # 1. Delete stress deployment.  Also cleaned up by namespace teardown,
        #    but explicit delete stops the DNS flood immediately.
        try:
            self.apps_v1.delete_namespaced_deployment(_STRESS_DEPLOYMENT, self.namespace, grace_period_seconds=0)
            print("Deleted DNS stress deployment.")
        except ApiException as e:
            if e.status == 404:
                print("DNS stress deployment already absent.")
            else:
                print(f"Error deleting stress deployment: {e}")

        # 2. Restore CoreDNS to its pre-injection state using the saved annotation.
        _restore_coredns_from_annotation(self.apps_v1, self.kubectl)

    # ------------------------------------------------------------------
    # CoreDNS annotation helpers
    # ------------------------------------------------------------------
    def _save_coredns_original(self):
        """Snapshot current CoreDNS config into an annotation on the deployment.

        If the annotation already exists (e.g. re-injection after a crash),
        the save is skipped to preserve the true original state.
        """
        try:
            dep = self.apps_v1.read_namespaced_deployment(_COREDNS_DEPLOYMENT, _COREDNS_NAMESPACE)
        except Exception as e:
            print(f"Failed to read CoreDNS deployment: {e}")
            return

        annotations = dep.metadata.annotations or {}
        if _ORIGINAL_CONFIG_ANNOTATION in annotations:
            print("Original CoreDNS config annotation already exists; skipping save.")
            return

        original = {"replicas": dep.spec.replicas or 2, "resources": {}}
        for container in dep.spec.template.spec.containers:
            if container.name == _COREDNS_DEPLOYMENT:
                res = container.resources
                original["resources"] = {
                    "limits": res.limits if res and res.limits else {},
                    "requests": res.requests if res and res.requests else {},
                }
                break

        annotations[_ORIGINAL_CONFIG_ANNOTATION] = json.dumps(original)
        try:
            self.apps_v1.patch_namespaced_deployment(
                _COREDNS_DEPLOYMENT,
                _COREDNS_NAMESPACE,
                {"metadata": {"annotations": annotations}},
            )
            print("Saved original CoreDNS config as annotation on the deployment.")
        except Exception as e:
            print(f"Failed to save CoreDNS annotation: {e}")


def _restore_coredns_from_annotation(apps_v1, kubectl):
    """Restore CoreDNS to its pre-injection state using the saved annotation.

    This is a module-level function so both the Problem class and the
    conductor's ``fix_kubernetes`` safety net can call it without
    instantiating a full Problem.
    """
    try:
        dep = apps_v1.read_namespaced_deployment(_COREDNS_DEPLOYMENT, _COREDNS_NAMESPACE)
    except Exception as e:
        print(f"Could not read CoreDNS deployment: {e}")
        return

    annotations = dep.metadata.annotations or {}
    raw = annotations.get(_ORIGINAL_CONFIG_ANNOTATION)
    if not raw:
        print("No original CoreDNS config annotation found; nothing to restore.")
        return

    original = json.loads(raw)

    resources = original.get("resources", {})
    if "limits" not in resources:
        resources["limits"] = {}

    # Set to None if 'cpu' wasn't part of the original limits config
    if "cpu" not in resources["limits"]:
        resources["limits"]["cpu"] = None

    patch_body = {
        "metadata": {"annotations": {_ORIGINAL_CONFIG_ANNOTATION: None}},
        "spec": {
            "replicas": original.get("replicas", 2),
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": _COREDNS_DEPLOYMENT,
                            "resources": resources,
                        }
                    ]
                }
            },
        },
    }
    try:
        apps_v1.patch_namespaced_deployment(_COREDNS_DEPLOYMENT, _COREDNS_NAMESPACE, patch_body)
        kubectl.exec_command(f"kubectl rollout restart deployment {_COREDNS_DEPLOYMENT} -n {_COREDNS_NAMESPACE}")
        kubectl.exec_command(
            f"kubectl rollout status deployment {_COREDNS_DEPLOYMENT} -n {_COREDNS_NAMESPACE} --timeout=60s"
        )
        print("Restored CoreDNS to original configuration and removed annotation.")
    except Exception as e:
        print(f"Error restoring CoreDNS: {e}")
