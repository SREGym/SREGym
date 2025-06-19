"""Inject faults at the virtualization layer: K8S, Docker, etc."""

import copy
import time

import yaml

from srearena.generators.fault.base import FaultInjector
from srearena.paths import TARGET_MICROSERVICES
from srearena.service.apps.base import Application
from srearena.service.helm import Helm
from srearena.service.kubectl import KubeCtl


class VirtualizationFaultInjector(FaultInjector):
    def __init__(self, namespace: str):
        super().__init__(namespace)
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self.mongo_service_pod_map = {
            "url-shorten-mongodb": "url-shorten-service",
        }

    def delete_service_pods(self, target_service_pods: list[str]):
        """Kill the corresponding service pod to enforce the fault."""
        for pod in target_service_pods:
            delete_pod_command = f"kubectl delete pod {pod} -n {self.namespace}"
            delete_result = self.kubectl.exec_command(delete_pod_command)
            print(f"Deleted service pod {pod} to enforce the fault: {delete_result}")

    ############# FAULT LIBRARY ################

    # V.1 - misconfig_k8s: Misconfigure service port in Kubernetes - Misconfig
    def inject_misconfig_k8s(self, microservices: list[str]):
        """Inject a fault to misconfigure service's target port in Kubernetes."""
        for service in microservices:
            service_config = self._modify_target_port_config(
                from_port=9090,
                to_port=9999,
                configs=self.kubectl.get_service_json(service, self.testbed),
            )

            print(f"Misconfig fault for service: {service} | namespace: {self.testbed}")
            self.kubectl.patch_service(service, self.testbed, service_config)

    def recover_misconfig_k8s(self, microservices: list[str]):
        for service in microservices:
            service_config = self._modify_target_port_config(
                from_port=9999,
                to_port=9090,
                configs=self.kubectl.get_service_json(service, self.testbed),
            )

            print(f"Recovering for service: {service} | namespace: {self.testbed}")
            self.kubectl.patch_service(service, self.testbed, service_config)

    # V.2 - auth_miss_mongodb: Authentication missing for MongoDB - Auth
    def inject_auth_miss_mongodb(self, microservices: list[str]):
        """Inject a fault to enable TLS for a MongoDB service.

        NOTE: modifies the values.yaml file for the service. The fault is created
        by forcing the service to require TLS for connections, which will fail if
        the certificate is not provided.

        NOTE: mode: requireTLS, certificateKeyFile, and CAFile are required fields.
        """
        for service in microservices:
            # Prepare the set values for helm upgrade
            set_values = {
                "url-shorten-mongodb.tls.mode": "requireTLS",
                "url-shorten-mongodb.tls.certificateKeyFile": "/etc/tls/tls.pem",
                "url-shorten-mongodb.tls.CAFile": "/etc/tls/ca.crt",
            }

            # Define Helm upgrade configurations
            helm_args = {
                "release_name": "social-network",
                "chart_path": TARGET_MICROSERVICES / "socialNetwork/helm-chart/socialnetwork/",
                "namespace": self.namespace,
                "values_file": TARGET_MICROSERVICES / "socialNetwork/helm-chart/socialnetwork/values.yaml",
                "set_values": set_values,
            }

            Helm.upgrade(**helm_args)

            pods = self.kubectl.list_pods(self.namespace)
            target_service_pods = [
                pod.metadata.name for pod in pods.items if self.mongo_service_pod_map[service] in pod.metadata.name
            ]
            print(f"Target Service Pods: {target_service_pods}")
            self.delete_service_pods(target_service_pods)

            self.kubectl.exec_command(f"kubectl rollout restart deployment {service} -n {self.namespace}")

    def recover_auth_miss_mongodb(self, microservices: list[str]):
        for service in microservices:
            set_values = {
                "url-shorten-mongodb.tls.mode": "disabled",
                "url-shorten-mongodb.tls.certificateKeyFile": "",
                "url-shorten-mongodb.tls.CAFile": "",
            }

            helm_args = {
                "release_name": "social-network",
                "chart_path": TARGET_MICROSERVICES / "socialNetwork/helm-chart/socialnetwork/",
                "namespace": self.namespace,
                "values_file": TARGET_MICROSERVICES / "socialNetwork/helm-chart/socialnetwork/values.yaml",
                "set_values": set_values,
            }

            Helm.upgrade(**helm_args)

            pods = self.kubectl.list_pods(self.namespace)
            target_service_pods = [
                pod.metadata.name for pod in pods.items if self.mongo_service_pod_map[service] in pod.metadata.name
            ]
            print(f"Target Service Pods: {target_service_pods}")

            self.delete_service_pods(target_service_pods)
            self.kubectl.exec_command(f"kubectl rollout restart deployment {service} -n {self.namespace}")

    # V.3 - scale_pods_to_zero: Scale pods to zero - Deploy/Operation
    def inject_scale_pods_to_zero(self, microservices: list[str]):
        """Inject a fault to scale pods to zero for a service."""
        for service in microservices:
            self.kubectl.exec_command(f"kubectl scale deployment {service} --replicas=0 -n {self.namespace}")
            print(f"Scaled deployment {service} to 0 replicas | namespace: {self.namespace}")

    def recover_scale_pods_to_zero(self, microservices: list[str]):
        for service in microservices:
            self.kubectl.exec_command(f"kubectl scale deployment {service} --replicas=1 -n {self.namespace}")
            print(f"Scaled deployment {service} back to 1 replica | namespace: {self.namespace}")

    # V.4 - assign_to_non_existent_node: Assign to non-existent or NotReady node - Dependency
    def inject_assign_to_non_existent_node(self, microservices: list[str]):
        """Inject a fault to assign a service to a non-existent or NotReady node."""
        non_existent_node_name = "extra-node"
        for service in microservices:
            deployment_yaml = self._get_deployment_yaml(service)
            deployment_yaml["spec"]["template"]["spec"]["nodeSelector"] = {
                "kubernetes.io/hostname": non_existent_node_name
            }

            # Write the modified YAML to a temporary file
            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            self.kubectl.exec_command(delete_command)

            apply_command = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"
            self.kubectl.exec_command(apply_command)
            print(f"Redeployed {service} to node {non_existent_node_name}.")

    def recover_assign_to_non_existent_node(self, microservices: list[str]):
        for service in microservices:
            deployment_yaml = self._get_deployment_yaml(service)
            if "nodeSelector" in deployment_yaml["spec"]["template"]["spec"]:
                del deployment_yaml["spec"]["template"]["spec"]["nodeSelector"]

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            self.kubectl.exec_command(delete_command)

            apply_command = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"
            self.kubectl.exec_command(apply_command)
            print(f"Removed nodeSelector for service {service} and redeployed.")

    # V.5 - redeploy without deleting the PV - only for HotelReservation
    def inject_redeploy_without_pv(self, app: Application):
        """Inject a fault to delete the namespace without deleting the PV."""
        self.kubectl.delete_namespace(self.namespace)
        print(f"Deleting namespace {self.namespace} without deleting the PV.")
        time.sleep(15)
        print(f"Redeploying {self.namespace}.")
        app = type(app)()
        app.deploy_without_wait()

    def recover_redeploy_without_pv(self, app: Application):
        app.cleanup()
        # pass

    # V.6 - wrong binary usage incident
    def inject_wrong_bin_usage(self, microservices: list[str]):
        """Inject a fault to use the wrong binary of a service."""
        for service in microservices:
            deployment_yaml = self._get_deployment_yaml(service)

            # Modify the deployment YAML to use the 'geo' binary instead of the 'profile' binary
            containers = deployment_yaml["spec"]["template"]["spec"]["containers"]
            for container in containers:
                if "command" in container and "profile" in container["command"]:
                    print(f"Changing binary for container {container['name']} from 'profile' to 'geo'.")
                    container["command"] = ["geo"]  # Replace 'profile' with 'geo'

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            # Delete the deployment and re-apply
            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_command = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"
            self.kubectl.exec_command(delete_command)
            self.kubectl.exec_command(apply_command)

            print(f"Injected wrong binary usage fault for service: {service}")

    def recover_wrong_bin_usage(self, microservices: list[str]):
        for service in microservices:
            deployment_yaml = self._get_deployment_yaml(service)

            containers = deployment_yaml["spec"]["template"]["spec"]["containers"]
            for container in containers:
                if "command" in container and "geo" in container["command"]:
                    print(f"Reverting binary for container {container['name']} from 'geo' to 'profile'.")
                    container["command"] = ["profile"]  # Restore 'geo' back to 'profile'

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_command = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"
            self.kubectl.exec_command(delete_command)
            self.kubectl.exec_command(apply_command)

            print(f"Recovered from wrong binary usage fault for service: {service}")

    # V.7 - Inject a fault by deleting the specified service
    def inject_missing_service(self, microservices: list[str]):
        """Inject a fault by deleting the specified service."""
        for service in microservices:
            service_yaml_file = self._get_service_yaml(service)
            delete_service_command = f"kubectl delete service {service} -n {self.namespace}"
            result = self.kubectl.exec_command(delete_service_command)
            print(f"Deleted service {service} to enforce the fault: {result}")

            self._write_yaml_to_file(service, service_yaml_file)

        # Restart all the pods
        self.kubectl.exec_command(f"kubectl delete pods --all -n {self.namespace}")
        self.kubectl.wait_for_stable(namespace=self.namespace)

    def recover_missing_service(self, microservices: list[str]):
        """Recover the fault by recreating the specified service."""
        for service in microservices:
            delete_service_command = f"kubectl delete service {service} -n {self.namespace}"
            result = self.kubectl.exec_command(delete_service_command)
            create_service_command = f"kubectl apply -f /tmp/{service}_modified.yaml -n {self.namespace}"
            result = self.kubectl.exec_command(create_service_command)
            print(f"Recreated service {service} to recover from the fault: {result}")

    # V.8 - Inject a fault by modifying the resource request of a service
    def inject_resource_request(self, microservices: list[str], memory_limit_func):
        """Inject a fault by modifying the resource request of a service."""
        for service in microservices:
            original_deployment_yaml = self._get_deployment_yaml(service)
            deployment_yaml = memory_limit_func(original_deployment_yaml)
            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            # Delete the deployment and re-apply
            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_command = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"
            self.kubectl.exec_command(delete_command)
            self.kubectl.exec_command(apply_command)

            self._write_yaml_to_file(service, original_deployment_yaml)

    def recover_resource_request(self, microservices: list[str]):
        """Recover the fault by restoring the original resource request of a service."""
        for service in microservices:
            # Delete the deployment and re-apply
            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_command = f"kubectl apply -f /tmp/{service}_modified.yaml -n {self.namespace}"
            self.kubectl.exec_command(delete_command)
            self.kubectl.exec_command(apply_command)

            print(f"Recovered from resource request fault for service: {service}")

    # V.9 - Manually patch a service's selector to include an additional label
    def inject_wrong_service_selector(self, microservices: list[str]):
        for service in microservices:
            print(f"Injecting wrong selector for service: {service} | namespace: {self.namespace}")

            service_config = self.kubectl.get_service_json(service, self.namespace)
            current_selectors = service_config.get("spec", {}).get("selector", {})

            # Adding a wrong selector to the service
            current_selectors["current_service_name"] = service
            service_config["spec"]["selector"] = current_selectors
            self.kubectl.patch_service(service, self.namespace, service_config)

            print(f"Patched service {service} with selector {service_config['spec']['selector']}")

    def recover_wrong_service_selector(self, microservices: list[str]):
        for service in microservices:
            service_config = self.kubectl.get_service_json(service, self.namespace)

            service_config = self.kubectl.get_service_json(service, self.namespace)
            current_selectors = service_config.get("spec", {}).get("selector", {})

            # Set the key to None to delete it from the live object
            current_selectors["current_service_name"] = None
            service_config["spec"]["selector"] = current_selectors
            self.kubectl.patch_service(service, self.namespace, service_config)

            print(f"Recovered from wrong service selector fault for service: {service}")

    # V.10 - Inject service DNS resolution failure by patching CoreDNS ConfigMap
    def inject_service_dns_resolution_failure(self, microservices: list[str]):
        for service in microservices:
            fqdn = f"{service}.{self.namespace}.svc.cluster.local"

            # Get configmap as structured data
            cm_yaml = self.kubectl.exec_command("kubectl -n kube-system get cm coredns -o yaml")
            cm_data = yaml.safe_load(cm_yaml)
            corefile = cm_data["data"]["Corefile"]

            start_line_id = f"template ANY ANY {fqdn} {{"
            if start_line_id in corefile:
                print("NXDOMAIN template already present; recovering from previous injection")
                self.recover_service_dns_resolution_failure([service])

                # Re-fetch after recovery
                cm_yaml = self.kubectl.exec_command("kubectl -n kube-system get cm coredns -o yaml")
                cm_data = yaml.safe_load(cm_yaml)
                corefile = cm_data["data"]["Corefile"]

            # Create the NXDOMAIN template block
            template_block = (
                f"    template ANY ANY {fqdn} {{\n"
                f'        match "^{fqdn}\\.$"\n'
                f"        rcode NXDOMAIN\n"
                f"        fallthrough\n"
                f"    }}\n"
            )

            # Find the position of "kubernetes" word
            kubernetes_pos = corefile.find("kubernetes")
            if kubernetes_pos == -1:
                print("Could not locate 'kubernetes' plugin in Corefile")
                return

            # Find the start of the line containing "kubernetes"
            line_start = corefile.rfind("\n", 0, kubernetes_pos)
            if line_start == -1:
                line_start = 0
            else:
                line_start += 1

            # Insert template block before the kubernetes line
            new_corefile = corefile[:line_start] + template_block + corefile[line_start:]

            cm_data["data"]["Corefile"] = new_corefile

            # Apply using temporary file
            tmp_file_path = self._write_yaml_to_file("coredns", cm_data)

            self.kubectl.exec_command(f"kubectl apply -f {tmp_file_path}")

            # Restart CoreDNS
            self.kubectl.exec_command("kubectl -n kube-system rollout restart deployment coredns")
            self.kubectl.exec_command("kubectl -n kube-system rollout status deployment coredns --timeout=30s")

            print(f"Injected Service DNS Resolution Failure fault for service: {service}")

    def recover_service_dns_resolution_failure(self, microservices: list[str]):
        for service in microservices:
            fqdn = f"{service}.{self.namespace}.svc.cluster.local"

            # Get configmap as structured data
            cm_yaml = self.kubectl.exec_command("kubectl -n kube-system get cm coredns -o yaml")
            cm_data = yaml.safe_load(cm_yaml)
            corefile = cm_data["data"]["Corefile"]

            start_line_id = f"template ANY ANY {fqdn} {{"
            if start_line_id not in corefile:
                print("No NXDOMAIN template found; nothing to do")
                return

            lines = corefile.split("\n")
            new_lines = []
            skip_block = False

            for line in lines:
                # Start of template block
                if not skip_block and start_line_id in line:
                    skip_block = True
                    continue

                # End of template block
                if skip_block and line.strip() == "}":
                    skip_block = False
                    continue

                # Skip lines inside the block
                if skip_block:
                    continue

                # Keep all other lines
                new_lines.append(line)

            if skip_block:
                print("WARNING: Template block was not properly closed")
                return

            new_corefile = "\n".join(new_lines)

            # Verify if the removal worked
            if start_line_id in new_corefile:
                print("ERROR: Template was not successfully removed!")
                return

            cm_data["data"]["Corefile"] = new_corefile

            # Apply using temporary file
            tmp_file_path = self._write_yaml_to_file("coredns", cm_data)
            self.kubectl.exec_command(f"kubectl apply -f {tmp_file_path}")

            # Restart CoreDNS
            self.kubectl.exec_command("kubectl -n kube-system rollout restart deployment coredns")
            self.kubectl.exec_command("kubectl -n kube-system rollout status deployment coredns --timeout=30s")

            print(f"Recovered Service DNS Resolution Failure fault for service: {service}")

    # V.11 - Inject a fault by modifying the DNS policy of a service
    def inject_wrong_dns_policy(self, microservices: list[str]):
        for service in microservices:
            patch = (
                '[{"op":"replace","path":"/spec/template/spec/dnsPolicy","value":"None"},'
                '{"op":"add","path":"/spec/template/spec/dnsConfig","value":'
                '{"nameservers":["8.8.8.8"],"searches":[]}}]'
            )
            patch_cmd = f"kubectl patch deployment {service} -n {self.namespace} --type json -p '{patch}'"
            result = self.kubectl.exec_command(patch_cmd)
            print(f"Patch result for {service}: {result}")

            self.kubectl.exec_command(f"kubectl rollout restart deployment {service} -n {self.namespace}")
            self.kubectl.exec_command(f"kubectl rollout status deployment {service} -n {self.namespace}")

            # Check if nameserver 8.8.8.8 present in the pods
            self._wait_for_dns_policy_propagation(service, external_ns="8.8.8.8", expect_external=True)

            print(f"Injected wrong DNS policy fault for service: {service}")

    def recover_wrong_dns_policy(self, microservices: list[str]):
        for service in microservices:
            patch = (
                '[{"op":"remove","path":"/spec/template/spec/dnsPolicy"},'
                '{"op":"remove","path":"/spec/template/spec/dnsConfig"}]'
            )
            patch_cmd = f"kubectl patch deployment {service} -n {self.namespace} --type json -p '{patch}'"
            result = self.kubectl.exec_command(patch_cmd)
            print(f"Patch result for {service}: {result}")

            self.kubectl.exec_command(f"kubectl rollout restart deployment {service} -n {self.namespace}")
            self.kubectl.exec_command(f"kubectl rollout status deployment {service} -n {self.namespace}")

            # Check if nameserver 8.8.8.8 absent in the pods
            self._wait_for_dns_policy_propagation(service, external_ns="8.8.8.8", expect_external=False)

            print(f"Recovered wrong DNS policy fault for service: {service}")

    # V.12 - Inject a stale CoreDNS config breaking all .svc.cluster.local DNS resolution
    def inject_stale_coredns_config(self, microservices: list[str] = None):
        # Get configmap as structured data
        cm_yaml = self.kubectl.exec_command("kubectl -n kube-system get cm coredns -o yaml")
        cm_data = yaml.safe_load(cm_yaml)
        corefile = cm_data["data"]["Corefile"]

        # Check if our template is already present (look for the exact line we inject)
        template_id = "template ANY ANY svc.cluster.local"
        if template_id in corefile:
            print("Cluster DNS failure template already present; recovering from previous injection")
            self.recover_stale_coredns_config()

            # Re-fetch after recovery
            cm_yaml = self.kubectl.exec_command("kubectl -n kube-system get cm coredns -o yaml")
            cm_data = yaml.safe_load(cm_yaml)
            corefile = cm_data["data"]["Corefile"]

        # Create the NXDOMAIN template block
        template_block = (
            "    template ANY ANY svc.cluster.local {\n"
            '        match ".*\\.svc\\.cluster\\.local\\.?$"\n'
            "        rcode NXDOMAIN\n"
            "    }\n"
        )

        # Find the position of "kubernetes" word
        kubernetes_pos = corefile.find("kubernetes")
        if kubernetes_pos == -1:
            print("Could not locate 'kubernetes' plugin in Corefile")
            return

        # Find the start of the line containing "kubernetes"
        line_start = corefile.rfind("\n", 0, kubernetes_pos)
        if line_start == -1:
            line_start = 0
        else:
            line_start += 1

        # Insert template block before the kubernetes line
        new_corefile = corefile[:line_start] + template_block + corefile[line_start:]

        cm_data["data"]["Corefile"] = new_corefile

        # Apply using temporary file
        tmp_file_path = self._write_yaml_to_file("coredns", cm_data)

        self.kubectl.exec_command(f"kubectl apply -f {tmp_file_path}")

        # Restart CoreDNS
        self.kubectl.exec_command("kubectl -n kube-system rollout restart deployment coredns")
        self.kubectl.exec_command("kubectl -n kube-system rollout status deployment coredns --timeout=30s")

        print("Injected stale CoreDNS config for all .svc.cluster.local domains")

    def recover_stale_coredns_config(self, microservices: list[str] = None):

        # Get configmap as structured data
        cm_yaml = self.kubectl.exec_command("kubectl -n kube-system get cm coredns -o yaml")
        cm_data = yaml.safe_load(cm_yaml)
        corefile = cm_data["data"]["Corefile"]

        # Check if our template is present
        template_id = "template ANY ANY svc.cluster.local"
        if template_id not in corefile:
            print("No cluster DNS failure template found; nothing to do")
            return

        lines = corefile.split("\n")
        new_lines = []
        skip_block = False

        for line in lines:
            # Start of template block
            if not skip_block and template_id in line:
                skip_block = True
                continue

            # End of template block
            if skip_block and line.strip() == "}":
                skip_block = False
                continue

            # Skip lines inside the block
            if skip_block:
                continue

            # Keep all other lines
            new_lines.append(line)

        if skip_block:
            print("WARNING: Template block was not properly closed")
            return

        new_corefile = "\n".join(new_lines)

        # Verify if the removal worked
        if template_id in new_corefile:
            print("ERROR: Template was not successfully removed!")
            return

        cm_data["data"]["Corefile"] = new_corefile

        # Apply using temporary file
        tmp_file_path = self._write_yaml_to_file("coredns", cm_data)
        self.kubectl.exec_command(f"kubectl apply -f {tmp_file_path}")

        # Restart CoreDNS
        self.kubectl.exec_command("kubectl -n kube-system rollout restart deployment coredns")
        self.kubectl.exec_command("kubectl -n kube-system rollout status deployment coredns --timeout=30s")

        print("Recovered from stale CoreDNS config for all .svc.cluster.local domains")

    # V.13 - Inject a sidecar container that binds to the same port as the main container (port conflict)
    def inject_sidecar_port_conflict(self, microservices: list[str]):
        for service in microservices:

            original_deployment_yaml = self._get_deployment_yaml(service)
            deployment_yaml = copy.deepcopy(original_deployment_yaml)

            containers = deployment_yaml["spec"]["template"]["spec"]["containers"]

            main_container = containers[0] if containers else {}
            default_port = 8080
            port = default_port
            ports_list = main_container.get("ports", [])
            if ports_list:
                port = ports_list[0].get("containerPort", default_port)

            sidecar_container = {
                "name": "sidecar",
                "image": "busybox:latest",
                "command": [
                    "sh",
                    "-c",
                    f"exec nc -lk -p {port}",
                ],
                "ports": [
                    {
                        "containerPort": port,
                    }
                ],
            }

            containers.append(sidecar_container)

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            delete_cmd = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_cmd = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_cmd)
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(apply_cmd)
            print(f"Apply result for {service}: {apply_result}")

            # Save the *original* deployment YAML for recovery
            self._write_yaml_to_file(service, original_deployment_yaml)

            self.kubectl.wait_for_stable(self.namespace)

            print(f"Injected sidecar port conflict fault for service: {service}")

    def recover_sidecar_port_conflict(self, microservices: list[str]):
        for service in microservices:
            delete_cmd = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_cmd = f"kubectl apply -f /tmp/{service}_modified.yaml -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_cmd)
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(apply_cmd)
            print(f"Apply result for {service}: {apply_result}")

            self.kubectl.wait_for_ready(self.namespace)

            print(f"Recovered from sidecar port conflict fault for service: {service}")

    ############# HELPER FUNCTIONS ################
    def _wait_for_pods_ready(self, microservices: list[str], timeout: int = 30):
        for service in microservices:
            command = (
                f"kubectl wait --for=condition=ready pod -l app={service} -n {self.namespace} --timeout={timeout}s"
            )
            result = self.kubectl.exec_command(command)
            print(f"Wait result for {service}: {result}")

    def _modify_target_port_config(self, from_port: int, to_port: int, configs: dict):
        for port in configs["spec"]["ports"]:
            if port.get("targetPort") == from_port:
                port["targetPort"] = to_port

        return configs

    def _get_values_yaml(self, service_name: str):
        kubectl = KubeCtl()
        values_yaml = kubectl.exec_command(f"kubectl get configmap {service_name} -n {self.testbed} -o yaml")
        return yaml.safe_load(values_yaml)

    def _enable_tls(self, values_yaml: dict):
        values_yaml["net"] = {
            "tls": {
                "mode": "requireTLS",
                "certificateKeyFile": "/etc/tls/tls.pem",
                "CAFile": "/etc/tls/ca.crt",
            }
        }
        return yaml.dump(values_yaml)

    def _apply_modified_yaml(self, service_name: str, modified_yaml: str):
        modified_yaml_path = f"/tmp/{service_name}-values.yaml"
        with open(modified_yaml_path, "w") as f:
            f.write(modified_yaml)

        kubectl = KubeCtl()
        kubectl.exec_command(
            f"kubectl create configmap {service_name} -n {self.testbed} --from-file=values.yaml={modified_yaml_path} --dry-run=client -o yaml | kubectl apply -f -"
        )
        kubectl.exec_command(f"kubectl rollout restart deployment {service_name} -n {self.testbed}")

    def _get_deployment_yaml(self, service_name: str):
        deployment_yaml = self.kubectl.exec_command(
            f"kubectl get deployment {service_name} -n {self.namespace} -o yaml"
        )
        return yaml.safe_load(deployment_yaml)

    def _get_service_yaml(self, service_name: str):
        deployment_yaml = self.kubectl.exec_command(f"kubectl get service {service_name} -n {self.namespace} -o yaml")
        return yaml.safe_load(deployment_yaml)

    def _change_node_selector(self, deployment_yaml: dict, node_name: str):
        if "spec" in deployment_yaml and "template" in deployment_yaml["spec"]:
            deployment_yaml["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": node_name}
        return yaml.dump(deployment_yaml)

    def _write_yaml_to_file(self, service_name: str, yaml_content: dict):
        """Helper function to write YAML content to a temporary file."""
        import yaml

        file_path = f"/tmp/{service_name}_modified.yaml"
        with open(file_path, "w") as file:
            yaml.dump(yaml_content, file)
        return file_path

    def _wait_for_dns_policy_propagation(
        self, service: str, external_ns: str, expect_external: bool, sleep: int = 2, max_wait: int = 120
    ):

        waited = 0
        while waited < max_wait:

            try:
                deploy = self.kubectl.apps_v1_api.read_namespaced_deployment(service, self.namespace)
                selector_dict = deploy.spec.selector.match_labels or {}
                label_selector = ",".join([f"{k}={v}" for k, v in selector_dict.items()]) if selector_dict else None
            except Exception:
                label_selector = None

            pods = self.kubectl.core_v1_api.list_namespaced_pod(self.namespace, label_selector=label_selector)

            target_pods = [pod.metadata.name for pod in pods.items if (label_selector or service in pod.metadata.name)]

            if not target_pods:
                time.sleep(sleep)
                waited += sleep
                continue

            state_ok = True

            for pod in target_pods:
                try:
                    resolv = self.kubectl.exec_command(
                        f"kubectl exec {pod} -n {self.namespace} -- cat /etc/resolv.conf"
                    )
                except Exception:
                    state_ok = False
                    break
                has_external = external_ns in resolv

                if expect_external != has_external:
                    state_ok = False
                    break

            if state_ok:
                return

            time.sleep(sleep)
            waited += sleep

        print(f"DNS policy propagation check for service '{service}' failed after {max_wait}s.")


if __name__ == "__main__":
    namespace = "test-social-network"
    microservices = ["mongodb-geo"]
    # microservices = ["geo"]
    fault_type = "auth_miss_mongodb"
    # fault_type = "misconfig_app"
    # fault_type = "revoke_auth"
    print("Start injection ...")
    injector = VirtualizationFaultInjector(namespace)
    # injector._inject(fault_type, microservices)
    injector._recover(fault_type, microservices)
