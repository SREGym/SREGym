import contextlib

import yaml

from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl


class ChaosInjector:
    def __init__(self, namespace: str):
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self.kubectl.create_namespace_if_not_exist("chaos-mesh")
        Helm.add_repo("chaos-mesh", "https://charts.chaos-mesh.org")
        chaos_configs = {
            "release_name": "chaos-mesh",
            "chart_path": "chaos-mesh/chaos-mesh",
            "namespace": "chaos-mesh",
            "version": "2.8.0",
        }

        container_runtime = self.kubectl.get_container_runtime()

        if "docker" in container_runtime:
            pass
        elif "containerd" in container_runtime:
            chaos_configs["extra_args"] = [
                "--set chaosDaemon.runtime=containerd",
                "--set chaosDaemon.socketPath=/run/containerd/containerd.sock",
            ]
        else:
            raise ValueError(f"Unsupported container runtime: {container_runtime}")

        # Disable security for the dashboard
        if chaos_configs.get("extra_args"):
            chaos_configs["extra_args"].append("--set dashboard.securityMode=false")
        else:
            chaos_configs["extra_args"] = ["--set dashboard.securityMode=false"]

        # Check if the release already exists
        release_exists = Helm.exists_release(chaos_configs["release_name"], chaos_configs["namespace"])
        if not release_exists:
            # Check for orphaned CRDs (CRDs without a helm release)
            crd_check = self.kubectl.exec_command("kubectl get crd 2>/dev/null | grep chaos-mesh.org || true")
            if crd_check and "chaos-mesh.org" in crd_check:
                print("[ChaosInjector] Found orphaned Chaos Mesh CRDs. Cleaning up before installation...")
                self._force_remove_chaos_mesh_crds()

            Helm.install(**chaos_configs)
            self.kubectl.wait_for_ready("chaos-mesh")
        else:
            print(
                f"[ChaosInjector] Helm release '{chaos_configs['release_name']}' already exists in namespace '{chaos_configs['namespace']}', skipping install."
            )

    def _force_remove_chaos_mesh_crds(self):
        """Strip finalizers from all chaos-mesh CRs, then delete the CRDs.

        Without this, CRDs with lingering CRs (whose finalizers reference the
        now-absent controller) will block indefinitely on deletion.
        """
        try:
            crd_output = self.kubectl.exec_command("kubectl get crd -o name 2>/dev/null | grep chaos-mesh.org || true")
        except Exception:
            return

        if not crd_output or "chaos-mesh.org" not in crd_output:
            return

        crd_names = [
            line.removeprefix("customresourcedefinition.apiextensions.k8s.io/")
            for line in crd_output.strip().splitlines()
            if line.strip()
        ]

        for crd in crd_names:
            resource = crd.split(".")[0]
            try:
                items = self.kubectl.exec_command(
                    f"kubectl get {resource}.chaos-mesh.org --all-namespaces "
                    f"-o jsonpath='{{range .items}}{{.metadata.namespace}}/{{.metadata.name}} {{end}}' "
                    f"2>/dev/null || true"
                )
            except Exception:
                continue
            for item in (items or "").split():
                item = item.strip()
                if not item or "/" not in item:
                    continue
                ns, name = item.split("/", 1)
                with contextlib.suppress(Exception):
                    self.kubectl.exec_command(
                        f"kubectl patch {resource}.chaos-mesh.org {name} -n {ns} "
                        f'--type merge -p \'{{"metadata":{{"finalizers":[]}}}}\' '
                        f"2>/dev/null || true"
                    )

        for crd in crd_names:
            with contextlib.suppress(Exception):
                self.kubectl.exec_command(f"kubectl delete crd {crd} --timeout=30s 2>/dev/null || true")

        print("[ChaosInjector] Force-removed all Chaos Mesh CRs and CRDs.")

    def create_chaos_experiment(self, experiment_yaml: dict, experiment_name: str):
        try:
            chaos_yaml_path = f"/tmp/{experiment_name}.yaml"
            with open(chaos_yaml_path, "w") as file:
                yaml.dump(experiment_yaml, file)
            command = f"kubectl apply -f {chaos_yaml_path}"
            result = self.kubectl.exec_command(command)
            print(f"Applied {experiment_name} chaos experiment: {result}")
            if "Error" in result or "error" in result or "denied" in result:
                raise RuntimeError(f"kubectl apply failed: {result}")
        except Exception as e:
            raise RuntimeError(f"Error applying chaos experiment: {e}") from e

    def delete_chaos_experiment(self, experiment_name: str):
        try:
            chaos_yaml_path = f"/tmp/{experiment_name}.yaml"
            command = f"kubectl delete -f {chaos_yaml_path}"
            result = self.kubectl.exec_command(command)
            print(f"Cleaned up chaos experiment: {result}")
        except Exception as e:
            chaos_yaml_path = f"/tmp/{experiment_name}.yaml"
            command = f"kubectl delete -f {chaos_yaml_path} --force --grace-period=0"
            result = self.kubectl.exec_command(command)
            raise RuntimeError(f"Error cleaning up chaos experiment: {e}") from e
