"""Interface to the FleetCast application (Ingress-enabled on first install; robust readiness checks)."""

import json
import os
import subprocess
import time
from pathlib import Path

from sregym.generators.workload.locust import LocustWorkloadManager
from sregym.observer import tidb_prometheus
from sregym.observer.tidb_cluster_deploy_helper import TiDBClusterDeployHelper
from sregym.observer.logstash.jaeger.jaeger import Jaeger
from sregym.paths import FLEET_CAST_METADATA
from sregym.service.apps.base import Application
from sregym.service.apps.tidb_cluster_operator import TiDBClusterDeployer
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl


class FleetCast(Application):
    def __init__(self):
        super().__init__(FLEET_CAST_METADATA)
        self.load_app_json()
        self.kubectl = KubeCtl()
        self.create_namespace()

    def _sh(self, cmd: str, check: bool = True, capture: bool = False) -> str:
        """Run a shell command; supports capture."""
        print(f"$ {cmd}")
        env = os.environ.copy()
        env.setdefault("HELM_MAX_CHART_FILE_SIZE", "104857600")  # 100 MiB
        if capture:
            return subprocess.check_output(cmd, shell=True, env=env).decode()
        subprocess.run(cmd, shell=True, check=check, env=env)
        return ""

    def load_app_json(self):
        super().load_app_json()
        metadata = self.get_app_json()
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"]

        self.frontend_service = "satellite-app-frontend"
        self.frontend_port = 80

    def ensure_ingress_controller(self):
        """Install nginx-ingress via Helm if missing, then wait until it's Running."""
        try:
            out = self._sh("kubectl get pods -n ingress-nginx -o json", capture=True)
            if len(json.loads(out).get("items", [])) == 0:
                raise RuntimeError("ingress-nginx empty")
            print("[ingress] ingress-nginx already present.")
        except Exception:
            self._sh("helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx || true")
            self._sh("helm repo update")
            self._sh(
                "helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx "
                "-n ingress-nginx --create-namespace "
                "--set controller.publishService.enabled=true"
            )

        print("[ingress] waiting for controller to be Running…")
        for _ in range(60):
            try:
                phase = (
                    self._sh(
                        "kubectl -n ingress-nginx get pods "
                        "-l app.kubernetes.io/name=ingress-nginx,app.kubernetes.io/component=controller "
                        "-o jsonpath='{.items[0].status.phase}'",
                        capture=True,
                    )
                    .strip()
                    .strip("'")
                )
                if phase == "Running":
                    print("[ingress] controller Running.")
                    return
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError("ingress-nginx controller did not become Ready in time.")

    def deploy(self):
        """Deploy TiDB, then install FleetCast chart from repo with Ingress enabled on the first install."""
        self.kubectl.create_namespace_if_not_exist(self.namespace)

        self.ensure_ingress_controller()

        print("Deploying TiDB Cluster with Operator...")
        base_dir = Path(__file__).parent.parent
        meta_path = base_dir / "metadata" / "tidb_metadata.json"
        TiDBClusterDeployHelper.running_cluster()
        print("---DEPLOYED TiDB CLUSTER---")

        Helm.add_repo("fleetcast", "https://lilygn.github.io/FleetCast")

        release = self.helm_configs.get("release_name", "fleetcast")
        fullname = f"{release}-satellite-app"
        fe_svc = f"{fullname}-frontend"
        be_svc = f"{fullname}-backend"

        ingress_args = [
            "--set-string",
            "ingress.enabled=true",
            "--set-string",
            "ingress.className=nginx",
            "--set-string",
            "ingress.hosts[0].host=orbital.local",
            "--set-string",
            "ingress.hosts[0].paths[0].path=/",
            "--set-string",
            "ingress.hosts[0].paths[0].pathType=Prefix",
            "--set-string",
            f"ingress.hosts[0].paths[0].backend.serviceName={fe_svc}",
            "--set",
            "ingress.hosts[0].paths[0].backend.servicePort=80",
            "--set-string",
            "ingress.hosts[0].paths[1].path=/api",
            "--set-string",
            "ingress.hosts[0].paths[1].pathType=Prefix",
            "--set-string",
            f"ingress.hosts[0].paths[1].backend.serviceName={be_svc}",
            "--set",
            "ingress.hosts[0].paths[1].backend.servicePort=5000",
        ]

        extra = self.helm_configs.get("extra_args", [])
        if isinstance(extra, str):
            extra = [extra]
        self.helm_configs["extra_args"] = extra + ingress_args

        Helm.install(**self.helm_configs)
        Helm.assert_if_deployed(self.helm_configs["namespace"])
        print("---DEPLOYED FLEETCAST---")

        self.wait_for_ingress_ready(ingress_name=f"{fullname}")
        self._print_access_hints()
        print("\n FleetCast deployment is complete and ready.")
        tidb_prometheus.main()
        print("PROMETHEUS: deployed TiDB monitoring stack.")
        Jaeger().deploy()

    def _get_ingress_svc_info(self) -> dict:
        """Return info about ingress-nginx-controller Service (type, external ip/hostname, nodePort for http/https)."""
        svc_json = self._sh(
            "kubectl -n ingress-nginx get svc ingress-nginx-controller -o json",
            capture=True,
        )
        data = json.loads(svc_json)
        info = {
            "type": data["spec"].get("type"),
            "external_ip": None,
            "external_hostname": None,
            "http_nodeport": None,
            "https_nodeport": None,
        }

        lb = data.get("status", {}).get("loadBalancer", {})
        ing = lb.get("ingress") or []
        if ing:
            info["external_ip"] = ing[0].get("ip")
            info["external_hostname"] = ing[0].get("hostname")

        for p in data["spec"].get("ports", []):
            name = p.get("name", "")
            if name == "http" or p.get("port") == 80:
                info["http_nodeport"] = p.get("nodePort")
            if name == "https" or p.get("port") == 443:
                info["https_nodeport"] = p.get("nodePort")

        return info

    def _print_access_hints(self):
        """Print friendly URLs based on current ingress Service shape."""
        info = self._get_ingress_svc_info()

        if info["type"] == "LoadBalancer" and (info["external_ip"] or info["external_hostname"]):
            host = info["external_ip"] or info["external_hostname"]
            print(f"\n FleetCast should be reachable at:  http://orbital.local/")
            print(f"   (map {host} to orbital.local in /etc/hosts)")
            print(f"   Backend health:                      http://orbital.local/api/health")
            print(f"   e.g., echo '{host} orbital.local' | sudo tee -a /etc/hosts")
            return

        np = info["http_nodeport"]
        if np:
            print(f"\n FleetCast should be reachable at:  http://orbital.local:{np}/")
            print(f"   Backend health:                      http://orbital.local:{np}/api/health")
            print("If developing locally, add a hosts entry:")
            print("  echo '<NODE-IP> orbital.local' | sudo tee -a /etc/hosts")
            print("Then open the URLs above (replace <NODE-IP> if you curl without the Host header).")
        else:
            print("\nℹ️ Could not detect NodePort for HTTP. Try a local forward:")
            print("  sudo kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 80:80")
            print("  # then use http://orbital.local/ after adding '127.0.0.1 orbital.local' to /etc/hosts")

    def wait_for_ingress_ready(self, ingress_name: str, timeout: int = 180):
        """Wait for Ingress to exist with rules and for ALL backend Services referenced to have endpoints."""
        ns = self.helm_configs.get("namespace", self.namespace)

        t0 = time.time()
        data = None
        while time.time() - t0 < timeout:
            try:
                raw = self._sh(f"kubectl get ingress {ingress_name} -n {ns} -o json", capture=True)
                data = json.loads(raw)
                rules = data.get("spec", {}).get("rules", [])
                if rules:
                    print("[ingress] Ingress has rules.")
                    break
            except Exception:
                pass
            time.sleep(2)
        else:
            raise RuntimeError(f"Ingress {ingress_name} not created with rules in time.")

        backend_svcs = set()
        for rule in data.get("spec", {}).get("rules", []):
            http = rule.get("http", {})
            for p in http.get("paths", []):
                svc = p.get("backend", {}).get("service", {})
                name = svc.get("name")
                if name:
                    backend_svcs.add(name)

        if not backend_svcs:
            raise RuntimeError("No backend services found in Ingress spec.")

        missing = []
        for svc in sorted(backend_svcs):
            ok = False
            t1 = time.time()
            while time.time() - t1 < timeout:
                try:
                    ed = json.loads(self._sh(f"kubectl get endpoints {svc} -n {ns} -o json", capture=True))
                    if any(s.get("addresses") for s in ed.get("subsets", [])):
                        ok = True
                        break
                except Exception:
                    pass
                time.sleep(2)
            if not ok:
                missing.append(svc)

        if missing:
            raise RuntimeError(f"Service endpoints missing for: {', '.join(missing)}")

        print("[ingress] All backend service endpoints are ready:", ", ".join(sorted(backend_svcs)))

    def delete(self):
        """Delete the Helm configurations."""
        Helm.uninstall(**self.helm_configs)
        self.kubectl.delete_namespace(self.helm_configs["namespace"])
        self.kubectl.wait_for_namespace_deletion(self.namespace)

    def cleanup(self):
        Helm.uninstall(**self.helm_configs)
        self.kubectl.delete_namespace(self.helm_configs["namespace"])
        if hasattr(self, "wrk"):
            self.wrk.stop()

    def create_workload(self):
        self.wrk = LocustWorkloadManager(
            namespace=self.namespace,
            locust_url="load-generator:8089",
        )

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.start()
