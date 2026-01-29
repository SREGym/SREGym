import json
import os
import subprocess
import time
from pathlib import Path
from textwrap import dedent

from sregym.paths import BASE_DIR
from sregym.service.helm import Helm


class TiDBClusterDeployer:
    def __init__(self, metadata_path):
        with open(metadata_path) as f:
            self.metadata = json.load(f)

        self.name = self.metadata["Name"]
        self.namespace_tidb_cluster = self.metadata["K8S Config"]["namespace"]
        self.cluster_config_url = self.metadata["K8S Config"]["config_url"]

        self.operator_namespace = self.metadata["Helm Operator Config"]["namespace"]
        self.operator_release_name = self.metadata["Helm Operator Config"]["release_name"]
        self.operator_chart = self.metadata["Helm Operator Config"]["chart_path"]
        self.operator_version = self.metadata["Helm Operator Config"]["version"]
        self.operator_crd_url = self.metadata["Helm Operator Config"]["CRD"]

        env_path = os.environ.get("TIDB_OPERATOR_VALUES")
        self.operator_values_path = ""
        if env_path and Path(env_path).expanduser().exists():
            self.operator_values_path = str(Path(env_path).expanduser().resolve())
        else:
            repo_root = Path(__file__).resolve().parents[3]
            candidates = [
                repo_root / "SREGym-applications/FleetCast/satellite-app/values.yaml",
                repo_root / "SREGym-applications/FleetCast/tidb-operator/values.yaml",
            ]
            for p in candidates:
                if p.exists():
                    self.operator_values_path = str(p.resolve())
                    break

        self.tidb_service = self.metadata.get("TiDB Service", "basic-tidb")
        self.tidb_port = int(self.metadata.get("TiDB Port", 4000))
        self.tidb_user = self.metadata.get("TiDB User", "root")

    def run_cmd(self, cmd):
        print(f"Running: {cmd}")
        subprocess.run(cmd, shell=True, check=True)

    def create_namespace(self, ns):
        self.run_cmd(f"kubectl create ns {ns} --dry-run=client -o yaml | kubectl apply -f -")

    def install_crds(self):
        print(f"Installing CRDs from {self.operator_crd_url} ...")
        self.run_cmd(f"kubectl create -f {self.operator_crd_url} || kubectl replace -f {self.operator_crd_url}")

    def apply_prometheus(self):
        ns = "observe"
        prom_yml_path = BASE_DIR / "SREGym-applications/FleetCast/prometheus/prometheus.yaml"

        prom_yml_path = str(prom_yml_path.resolve())
        if not os.path.isfile(prom_yml_path):
            raise FileNotFoundError(f"prometheus.yaml not found at {prom_yml_path}")

        self.run_cmd(
            f"kubectl -n {ns} create configmap prometheus-config "
            f"--from-file=prometheus.yml={prom_yml_path} "
            f"-o yaml --dry-run=client | kubectl apply -f -"
        )

        self.run_cmd(
            "kubectl -n observe port-forward svc/prometheus-server 9090:80 >/dev/null 2>&1 & "
            "PF=$!; sleep 1; curl -s -X POST http://127.0.0.1:9090/-/reload >/dev/null; kill $PF || true"
        )

        print(f"[ok] Prometheus config applied from {prom_yml_path}")

    def install_operator_with_values(self):
        print(f"Installing/upgrading TiDB Operator via Helm in namespace '{self.operator_namespace}'...")
        self.create_namespace(self.operator_namespace)

        # Add pingcap repo with retry logic to handle transient DNS/network issues
        try:
            Helm.add_repo("pingcap", "https://charts.pingcap.org")
        except RuntimeError as e:
            print(f"[warn] Failed to add pingcap repo after retries: {e}")
            print("[info] Continuing with cached charts if available")

        # Update repos with retry logic
        try:
            Helm.repo_update()
        except RuntimeError as e:
            print(f"[warn] Failed to update helm repos after retries: {e}")
            print("[info] Continuing with cached charts if available")

        values_arg = ""
        if self.operator_values_path:
            print(f"[info] Using values file: {self.operator_values_path}")
            values_arg = f"-f {self.operator_values_path}"
        else:
            print("[warn] No values.yaml found; installing with chart defaults")

        self.run_cmd(
            f"helm upgrade --install {self.operator_release_name} {self.operator_chart} "
            f"--version {self.operator_version} -n {self.operator_namespace} "
            f"--create-namespace {values_arg} "
        )

    def wait_for_operator_ready(self):
        print("Waiting for tidb-controller-manager pod to be running...")
        label = "app.kubernetes.io/component=controller-manager"
        for _ in range(24):
            try:
                status = (
                    subprocess.check_output(
                        f"kubectl get pods -n {self.operator_namespace} -l {label} -o jsonpath='{{.items[0].status.phase}}'",
                        shell=True,
                    )
                    .decode()
                    .strip()
                )
                if status == "Running":
                    print(" tidb-controller-manager pod is running.")
                    return
            except subprocess.CalledProcessError:
                pass
            print("-- Pod not ready yet, retrying in 5 seconds...")
            time.sleep(5)
        raise RuntimeError("--------Timeout waiting for tidb-controller-manager pod")

    def deploy_tidb_cluster(self):
        print(f"Creating TiDB cluster namespace '{self.namespace_tidb_cluster}'...")
        self.create_namespace(self.namespace_tidb_cluster)
        print(f"Deploying TiDB cluster manifest from {self.cluster_config_url}...")
        self.run_cmd(f"kubectl apply -f {self.cluster_config_url} -n {self.namespace_tidb_cluster}")

    def run_sql(self, sql_text: str):
        ns = self.namespace_tidb_cluster
        svc = f"{self.tidb_service}.{ns}.svc"
        port = self.tidb_port
        user = self.tidb_user

        self.run_cmd(f"kubectl -n {ns} delete pod/mysql-client --ignore-not-found")
        self.run_cmd(
            f"kubectl -n {ns} run mysql-client --image=mysql:8 --restart=Never --command -- sleep 3600 || true"
        )
        self.run_cmd(f"kubectl -n {ns} wait --for=condition=Ready pod/mysql-client --timeout=180s")

        sql = dedent(sql_text).strip()
        heredoc = f"""kubectl -n {ns} exec -i mysql-client -- bash -lc "cat <<'SQL' | mysql -h {svc} -P {port} -u{user}
{sql}
SQL"
"""
        self.run_cmd(heredoc)

        self.run_cmd(f"kubectl -n {ns} delete pod/mysql-client --wait=false || true")

    def init_schema_and_seed(self):
        print("Initializing schema and seeding data in satellite_sim ...")
        sql = """
        CREATE DATABASE IF NOT EXISTS satellite_sim;
        USE satellite_sim;

        DROP TABLE IF EXISTS telemetry;
        DROP TABLE IF EXISTS contact_windows;

        CREATE TABLE contact_windows (
          id BIGINT PRIMARY KEY AUTO_INCREMENT,
          satellite_id VARCHAR(20),
          ground_station_id VARCHAR(20),
          start_time DATETIME,
          end_time DATETIME,
          timestamp DATETIME,
          distance FLOAT,
          datavolume INT,
          priority INT,
          assigned BOOLEAN DEFAULT FALSE,
          KEY idx_active (assigned, end_time),
          KEY idx_gs_sat (ground_station_id, satellite_id)
        );

        CREATE TABLE telemetry (
          id BIGINT PRIMARY KEY AUTO_INCREMENT,
          satellite_id VARCHAR(20),
          ground_station_id VARCHAR(20),
          timestamp DATETIME,
          battery_level FLOAT,
          temperature FLOAT,
          position_lat FLOAT,
          position_lon FLOAT,
          status VARCHAR(20),
          KEY idx_sat_time (satellite_id, timestamp),
          KEY idx_gs_time  (ground_station_id, timestamp)
        );
        """
        self.run_sql(sql)

    def wait_for_pods_ready(self, selector: str, poll: float = 1.0):
        """
        Poll every `poll` seconds until ALL pods matching `selector` are Ready.
        Runs indefinitely until condition is met.
        """
        ns = self.namespace_tidb_cluster
        while True:
            try:
                out = subprocess.check_output(
                    f"kubectl -n {ns} get pods -l '{selector}' "
                    "-o jsonpath='{range .items[*]}{.metadata.name} "
                    '{range .status.containerStatuses[*]}{.ready} {end}{"\\n"}{end}\'',
                    shell=True,
                ).decode()

                lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
                if lines:
                    all_ready = True
                    for ln in lines:
                        parts = ln.split()
                        if not parts or not all(p.lower() == "true" for p in parts[1:]):
                            all_ready = False
                            break
                    if all_ready:
                        print(f"[ok] All pods with selector '{selector}' are Ready.")
                        return
            except subprocess.CalledProcessError:
                pass
            time.sleep(poll)

    def wait_for_basic_workloads(self):
        """
        Wait (no timeout) for PD, TiKV, and TiDB pods to be Ready,
        then wait for the TiDB Service to exist and have endpoints.
        """
        ns = self.namespace_tidb_cluster
        cluster = "basic"

        # 1) PD
        self.wait_for_pods_ready(selector="app.kubernetes.io/instance=basic,app.kubernetes.io/component=pd")

        # 2) TiKV
        self.wait_for_pods_ready(selector="app.kubernetes.io/instance=basic,app.kubernetes.io/component=tikv")

        # 3) TiDB
        self.wait_for_pods_ready(selector="app.kubernetes.io/instance=basic,app.kubernetes.io/component=tidb")

        try:
            svc_name = (
                subprocess.check_output(
                    f"kubectl -n {ns} get svc -l app.kubernetes.io/instance={cluster},"
                    "app.kubernetes.io/component=tidb "
                    "-o jsonpath='{.items[0].metadata.name}'",
                    shell=True,
                )
                .decode()
                .strip()
                .strip("'")
            )
            if not svc_name:
                svc_name = self.tidb_service
        except subprocess.CalledProcessError:
            svc_name = self.tidb_service

        print(f"[info] Using TiDB Service: {svc_name}")
        self.tidb_service = svc_name
        while True:
            try:
                eps = (
                    subprocess.check_output(
                        f"kubectl -n {ns} get endpoints {svc_name} "
                        "-o jsonpath='{range .subsets[*].addresses[*]}{.ip}{\"\\n\"}{end}'",
                        shell=True,
                    )
                    .decode()
                    .strip()
                    .strip("'")
                )
                if eps:
                    print(f"[ok] Service {svc_name} has endpoints:\n{eps}")
                    return
            except subprocess.CalledProcessError:
                pass
            time.sleep(1.0)

    def deploy_all(self):
        print(f"----------Starting deployment: {self.name}")
        self.create_namespace(self.namespace_tidb_cluster)
        self.install_crds()
        self.install_operator_with_values()
        self.wait_for_operator_ready()
        self.deploy_tidb_cluster()
        self.wait_for_basic_workloads()
        self.init_schema_and_seed()
        print("-------------TiDB cluster deployment complete.")


if __name__ == "__main__":
    deployer = TiDBClusterDeployer("../metadata/tidb_metadata.json")
    deployer.deploy_all()
