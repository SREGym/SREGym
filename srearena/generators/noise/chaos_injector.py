import os
from typing import List

import yaml
from kubernetes import client

from srearena.service.helm import Helm
from srearena.service.kubectl import KubeCtl

class ChaosInjector():
    def __init__(self, namespace: str):
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self.kubectl.create_namespace_if_not_exist("chaos-mesh")
        Helm.add_repo("chaos-mesh", "https://charts.chaos-mesh.org")
        chaos_configs = {
            "release_name": "chaos-mesh",
            "chart_path": "chaos-mesh/chaos-mesh",
            "namespace": "chaos-mesh",
            "version": "2.6.2",
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
            # create as a list (install expects a list)
            chaos_configs["extra_args"] = ["--set dashboard.securityMode=false"]

        Helm.install(**chaos_configs)

    def create_chaos_experiment(self, experiment_yaml: dict, experiment_name: str):
        try:
            chaos_yaml_path = f"/tmp/{experiment_name}.yaml"
            with open(chaos_yaml_path, "w") as file:
                yaml.dump(experiment_yaml, file)
            command = f"kubectl apply -f {chaos_yaml_path}"
            result = self.kubectl.exec_command(command)
            print(f"Applied {experiment_name} chaos experiment: {result}")
        except Exception as e:
            raise RuntimeError(f"Error applying chaos experiment: {e}") from e

    def delete_chaos_experiment(self, experiment_name: str):
        try:
            chaos_yaml_path = f"/tmp/{experiment_name}.yaml"
            command = f"kubectl delete -f {chaos_yaml_path}"
            result = self.kubectl.exec_command(command)
            print(f"Cleaned up chaos experiment: {result}")
        except Exception as e:
            raise RuntimeError(f"Error cleaning up chaos experiment: {e}") from e