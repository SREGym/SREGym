from typing import List

import yaml

from srearena.generators.fault.base import FaultInjector
from srearena.service.helm import Helm
from srearena.service.kubectl import KubeCtl


class SymptomFaultInjector(FaultInjector):
    def __init__(self, namespace: str):
        super().__init__(namespace)
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
        chaos_yaml_path = f"/tmp/{experiment_name}.yaml"
        with open(chaos_yaml_path, "w") as file:
            yaml.dump(experiment_yaml, file)
        command = f"kubectl apply -f {chaos_yaml_path}"
        result = self.kubectl.exec_command(command)
        print(f"Applied {experiment_name} chaos experiment: {result}")

    def delete_chaos_experiment(self, experiment_name: str):
        chaos_yaml_path = f"/tmp/{experiment_name}.yaml"
        command = f"kubectl delete -f {chaos_yaml_path}"
        result = self.kubectl.exec_command(command)
        print(f"Cleaned up chaos experiment: {result}")

    def recover_pod_failure(self):
        self.delete_chaos_experiment("pod-failure")

    def inject_pod_failure(self, microservices: List[str]):
        """
        Inject a pod failure fault.
        """
        chaos_experiment = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "PodChaos",
            "metadata": {"name": "pod-failure-experiment", "namespace": self.namespace},
            "spec": {
                "action": "pod-failure",
                "mode": "one",
                "selector": {"labelSelectors": {"io.kompose.service": ", ".join(microservices)}},
            },
        }

        self.create_chaos_experiment(chaos_experiment, "pod-failure")

    def recover_network_loss(self):
        self.delete_chaos_experiment("network-loss")

    def inject_network_loss(self, microservices: List[str]):
        """
        Inject a network loss fault.
        """
        chaos_experiment = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "NetworkChaos",
            "metadata": {"name": "loss", "namespace": self.namespace},
            "spec": {
                "action": "loss",
                "mode": "one",
                "selector": {
                    "namespaces": [self.namespace],
                    "labelSelectors": {"io.kompose.service": ", ".join(microservices)},
                },
                "loss": {"loss": "99", "correlation": "100"},
            },
        }

        self.create_chaos_experiment(chaos_experiment, "network-loss")

    def inject_container_kill(self, microservice: str, containers: List[str]):
        """
        Inject a container kill.
        """
        chaos_experiment = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "PodChaos",
            "metadata": {"name": "container-kill", "namespace": self.namespace},
            "spec": {
                "action": "container-kill",
                "mode": "one",
                "selector": {"labelSelectors": {"io.kompose.service": microservice}},
                "containerNames": (containers if isinstance(containers, list) else [containers]),
            },
        }

        self.create_chaos_experiment(chaos_experiment, "container-kill")

    def recover_container_kill(self):
        self.delete_chaos_experiment("container-kill")

    def inject_network_delay(
        self,
        microservices: List[str],
        latency: str = "10s",
        jitter: str = "0ms",
    ):
        """
        Inject a network delay fault.

        Args:
            microservices (List[str]): A list of microservice names or labels to target.
            latency (str): The amount of delay to introduce.
            jitter (str): The jitter for the delay.
        """
        chaos_experiment = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "NetworkChaos",
            "metadata": {"name": "delay", "namespace": self.namespace},
            "spec": {
                "action": "delay",
                "mode": "one",
                "selector": {"labelSelectors": {"io.kompose.service": ", ".join(microservices)}},
                "delay": {"latency": latency, "correlation": "100", "jitter": jitter},
            },
        }

        self.create_chaos_experiment(chaos_experiment, "network-delay")

    def recover_network_delay(self):
        self.delete_chaos_experiment("network-delay")

    def inject_network_partition(self, from_service: str, to_service: str):
        """
        Inject a network partition between two services using Chaos Mesh.

        Args:
            from_service (str): The service initiating the connection.
            to_service (str): The service that will be isolated from the initiator.
        """
        chaos_experiment = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "NetworkChaos",
            "metadata": {
                "name": f"network-partition-{from_service}-to-{to_service}",
                "namespace": self.namespace,
            },
            "spec": {
                "action": "partition",
                "mode": "all",
                "selector": {
                    "namespaces": [self.namespace],
                    "labelSelectors": {"app.kubernetes.io/component": from_service},
                },
                "direction": "to",
                "target": {
                    "mode": "all",
                    "selector": {
                        "namespaces": [self.namespace],
                        "labelSelectors": {"app.kubernetes.io/component": to_service},
                    },
                },
            },
        }

        self.create_chaos_experiment(chaos_experiment, f"network-partition-{from_service}-to-{to_service}")

    def recover_network_partition(self, from_service: str, to_service: str):
        """
        Recover from a network partition fault.

        Args:
            from_service (str): The service that initiated the partition.
            to_service (str): The service that was isolated.
        """
        self.delete_chaos_experiment(f"network-partition-{from_service}-to-{to_service}")

    def inject_pod_kill(self, microservices: List[str]):
        """
        Inject a pod kill fault targeting specified microservices by label in the configured namespace.

        Args:
            microservices (List[str]): A list of microservices labels to target for the pod kill experiment.
        """
        chaos_experiment = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "PodChaos",
            "metadata": {"name": "pod-kill", "namespace": self.namespace},
            "spec": {
                "action": "pod-kill",
                "mode": "one",
                "selector": {"labelSelectors": {"io.kompose.service": ", ".join(microservices)}},
            },
        }

        self.create_chaos_experiment(chaos_experiment, "pod-kill")

    def recover_pod_kill(self):
        self.delete_chaos_experiment("pod-kill")

    # IMPORTANT NOTE:
    # Kernel fault is not working and is a known bug in chaos-mesh 0> https://github.com/xlab-uiuc/agent-ops/pull/10#issuecomment-2468992285
    # This code is untested as we're waiting for a resolution to the bug to retry.
    def inject_kernel_fault(self, microservices: List[str]):
        """
        Injects a kernel fault targeting the specified function in the kernel call chain.
        """
        chaos_experiment = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "KernelChaos",
            "metadata": {"name": "kernel-chaos", "namespace": self.namespace},
            "spec": {
                "mode": "one",
                "selector": {"labelSelectors": {"io.kompose.service": ", ".join(microservices)}},
                "failKernRequest": {
                    "callchain": [{"funcname": "__x64_sys_mount"}],
                    "failtype": 0,
                },
            },
        }

        self.create_chaos_experiment(chaos_experiment, "kernel-chaos")

    def recover_kernel_fault(self):
        self.delete_chaos_experiment("kernel-chaos")

    def inject_cpu_stress(self, deployment_name: str, microservice: str):
        """
        Inject CPU stress fault using Chaos Mesh after reducing CPU limits on the deployment.
        """
        # Patch deployment's CPU settings to low limits
        patch = [
            {
                "op": "replace",
                "path": "/spec/template/spec/containers/0/resources/requests/cpu",
                "value": "2m",
            },
            {
                "op": "replace",
                "path": "/spec/template/spec/containers/0/resources/limits/cpu",
                "value": "2m",
            },
        ]
        patch_str = yaml.dump(patch)
        patch_cmd = f"kubectl patch deployment {deployment_name} " f"-n {self.namespace} --type='json' -p='{patch_str}'"
        print(f"Patching CPU limits for deployment {deployment_name}")
        self.kubectl.exec_command(patch_cmd)

        # Define and dump the StressChaos experiment
        experiment_name = f"cpu-stress-{microservice}"
        chaos_experiment = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "StressChaos",
            "metadata": {
                "name": experiment_name,
                "namespace": self.namespace,
            },
            "spec": {
                "mode": "all",
                "selector": {
                    "namespaces": [self.namespace],
                    "labelSelectors": {"app.kubernetes.io/component": microservice},
                },
                "stressors": {
                    "cpu": {
                        "workers": 30,
                        "load": 100,
                    }
                },
            },
        }

        self.create_chaos_experiment(chaos_experiment, experiment_name)

    def recover_cpu_stress(self, deployment_name: str, microservice: str):
        """
        Recover from CPU stress fault and restore original CPU limits.
        Deletes the YAML spec from /tmp/cpu-stress-{microservice}.yaml.
        """
        experiment_name = f"cpu-stress-{microservice}"
        self.delete_chaos_experiment(experiment_name)

        # Revert CPU requests/limits
        patch = [
            {
                "op": "replace",
                "path": "/spec/template/spec/containers/0/resources/requests/cpu",
                "value": "50m",
            },
            {
                "op": "remove",
                "path": "/spec/template/spec/containers/0/resources/limits/cpu",
            },
        ]
        patch_str = yaml.dump(patch)
        patch_cmd = f"kubectl patch deployment {deployment_name} " f"-n {self.namespace} --type='json' -p='{patch_str}'"
        print(f"Reverting CPU patch for deployment {deployment_name}")
        self.kubectl.exec_command(patch_cmd)

    def inject_http_abort(self, microservice: str, port: int = 8080, method: str = "POST"):
        """
        Inject an HTTP abort fault using Chaos Mesh HTTPChaos. Aborts all POST requests on a given port.

        Args:
            microservice (str): Label of the microservice to target.
            port (int): Port to intercept (default is 8080).
            method (str): HTTP method to abort (default is POST).
        """
        experiment_name = f"http-abort-{microservice}"
        chaos_experiment = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "HTTPChaos",
            "metadata": {
                "name": experiment_name,
                "namespace": self.namespace,
            },
            "spec": {
                "mode": "all",
                "selector": {
                    "namespaces": [self.namespace],
                    "labelSelectors": {
                        "app.kubernetes.io/component": microservice,
                    },
                },
                "target": "Request",
                "port": port,
                "method": method.upper(),
                "path": "*",
                "abort": True,
            },
        }

        self.create_chaos_experiment(chaos_experiment, experiment_name)

    def recover_http_abort(self, microservice: str):
        experiment_name = f"http-abort-{microservice}"
        self.delete_chaos_experiment(experiment_name)


if __name__ == "__main__":
    namespace = "test-hotel-reservation"
    microservices = ["geo"]
    fault_type = "pod_failure"
    injector = SymptomFaultInjector(namespace)
    injector._inject(fault_type, microservices, "30s")
    injector._recover(fault_type)
