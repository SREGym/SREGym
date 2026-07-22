"""Inject faults at the virtualization layer: K8S, Docker, etc."""

import copy
import json
import time
from pathlib import Path

import yaml
from kubernetes.client.rest import ApiException

from sregym.generators.fault.base import FaultInjector
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl


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

            # Scale down to 0 to terminate all healthy pods, then scale back up so only the faulty pod starts
            self.kubectl.exec_command(f"kubectl scale deployment {service} -n {self.namespace} --replicas=0")
            self.kubectl.exec_command(f"kubectl rollout status deployment {service} -n {self.namespace} --timeout=60s")
            self.kubectl.exec_command(f"kubectl scale deployment {service} -n {self.namespace} --replicas=1")

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

    # --- V.5 - PVC claim name mismatch (per-service) ---
    def inject_pvc_claim_mismatch(self, microservices: list[str]):
        """Make pods Pending by pointing Deployments at a non-existent PVC claim."""
        for service in microservices:
            dep = self._get_deployment_yaml(service)
            original = copy.deepcopy(dep)

            pod_spec = dep.get("spec", {}).get("template", {}).get("spec", {})
            volumes = pod_spec.get("volumes", [])
            changed = False

            for v in volumes:
                pvc = v.get("persistentVolumeClaim")
                if pvc and "claimName" in pvc:
                    pvc["claimName"] = pvc["claimName"] + "-broken"
                    changed = True

            if not changed:
                print(f"[{service}] No PVC volumes found; skipping.")
                continue

            modified = self._write_yaml_to_file(service, dep)

            # Replace the deployment with the modified one
            self.kubectl.exec_command(f"kubectl delete deployment {service} -n {self.namespace}")
            self.kubectl.exec_command(f"kubectl apply -f {modified} -n {self.namespace}")

            # Save the original for recovery
            self._write_yaml_to_file(service, original)

            print(f"[{service}] Patched claimName -> (…-broken). Pods should go Pending.")

        self.kubectl.wait_for_stable(self.namespace)

    def recover_pvc_claim_mismatch(self, microservices: list[str]):
        """Restore the original Deployment YAML saved in /tmp/{svc}_modified.yaml."""
        for service in microservices:
            orig_path = f"/tmp/{service}_modified.yaml"
            self.kubectl.exec_command(f"kubectl delete deployment {service} -n {self.namespace}")
            self.kubectl.exec_command(f"kubectl apply -f {orig_path} -n {self.namespace}")
            print(f"[{service}] Restored original claimName.")

        self.kubectl.wait_for_ready(self.namespace)

    # --- V.6 - Storage provisioner outage (cluster-scoped) ---
    # TODO: This fault does not work because the PVCs are bound before fault injection
    # def inject_storage_provisioner_outage(self):
    #     """
    #     Make all new PVCs Pending by disabling common local provisioners.
    #     No-op if a target provisioner isn't present.
    #     """
    #     cmds = [
    #         # OpenEBS localPV provisioner
    #         "kubectl -n openebs scale deploy openebs-localpv-provisioner --replicas=0",
    #         # Rancher/Kind local-path provisioner
    #         "kubectl -n local-path-storage scale deploy local-path-provisioner --replicas=0",
    #     ]
    #     for c in cmds:
    #         try:
    #             self.kubectl.exec_command(c)
    #             print(f"Ran: {c}")
    #         except Exception as e:
    #             print(f"Skipping: {c} ({e})")

    #     print("Storage provisioner outage injected.")

    # def recover_storage_provisioner_outage(self):
    #     cmds = [
    #         "kubectl -n openebs scale deploy openebs-localpv-provisioner --replicas=1",
    #         "kubectl -n local-path-storage scale deploy local-path-provisioner --replicas=1",
    #         "kubectl -n kube-system scale deploy hostpath-provisioner --replicas=1",
    #     ]
    #     for c in cmds:
    #         try:
    #             self.kubectl.exec_command(c)
    #             print(f"Ran: {c}")
    #         except Exception as e:
    #             print(f"Skipping: {c} ({e})")

    #     # Give the controller a moment and ensure PVCs start binding again
    #     self.kubectl.wait_for_stable(self.namespace)
    #     print("✅ Storage provisioner outage recovered.")

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

        # Restart all pods to clear cached DNS failures from the fault period
        self.kubectl.exec_command(f"kubectl delete pods --all -n {self.namespace}")
        self.kubectl.wait_for_stable(namespace=self.namespace)

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

    def inject_service_wrong_pod_selection(self, microservices: list[str]):
        if len(microservices) != 2:
            raise ValueError("service_wrong_pod_selection requires [target_service, wrong_deployment]")

        target_service = microservices[0]
        wrong_deployment = microservices[1]
        route_label_key = "service-route"
        route_label_value = target_service

        print(
            f"Injecting wrong pod selection for service: {target_service} | "
            f"wrong deployment: {wrong_deployment} | namespace: {self.namespace}"
        )

        for deployment in [target_service, wrong_deployment]:
            self.kubectl.patch_deployment(
                deployment,
                self.namespace,
                {
                    "spec": {
                        "template": {
                            "metadata": {
                                "labels": {
                                    route_label_key: route_label_value,
                                },
                            },
                        },
                    },
                },
            )
            self.kubectl.exec_command(
                f"kubectl rollout status deployment/{deployment} -n {self.namespace} --timeout=120s"
            )

        selector = {route_label_key: route_label_value}
        patch = json.dumps([{"op": "replace", "path": "/spec/selector", "value": selector}])
        self.kubectl.exec_command(f"kubectl patch svc {target_service} -n {self.namespace} --type=json -p='{patch}'")

        print(f"Patched service {target_service} with selector {selector}")

    def recover_service_wrong_pod_selection(self, microservices: list[str]):
        if len(microservices) != 2:
            raise ValueError("service_wrong_pod_selection requires [target_service, wrong_deployment]")

        target_service = microservices[0]
        wrong_deployment = microservices[1]
        route_label_key = "service-route"
        original_selector = {"io.kompose.service": target_service}

        patch = json.dumps([{"op": "replace", "path": "/spec/selector", "value": original_selector}])
        self.kubectl.exec_command(f"kubectl patch svc {target_service} -n {self.namespace} --type=json -p='{patch}'")

        for deployment in [target_service, wrong_deployment]:
            self.kubectl.patch_deployment(
                deployment,
                self.namespace,
                {
                    "spec": {
                        "template": {
                            "metadata": {
                                "labels": {
                                    route_label_key: None,
                                },
                            },
                        },
                    },
                },
            )
            self.kubectl.exec_command(
                f"kubectl rollout status deployment/{deployment} -n {self.namespace} --timeout=120s"
            )

        print(f"Recovered from wrong pod selection fault for service: {target_service}")

    # V.10 - Inject service DNS resolution failure by patching CoreDNS ConfigMap
    def inject_service_dns_resolution_failure(self, microservices: list[str]):
        for service in microservices:
            fqdn = f"{service}.{self.namespace}.svc.cluster.local"

            # Get configmap as structured data
            cm_yaml = self.kubectl.exec_command_checked("kubectl -n kube-system get cm coredns -o yaml")
            cm_data = yaml.safe_load(cm_yaml)
            corefile = cm_data["data"]["Corefile"]

            start_line_id = f"template ANY ANY {fqdn} {{"
            if start_line_id in corefile:
                print("NXDOMAIN template already present; recovering from previous injection")
                self.recover_service_dns_resolution_failure([service])

                # Re-fetch after recovery
                cm_yaml = self.kubectl.exec_command_checked("kubectl -n kube-system get cm coredns -o yaml")
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
                raise RuntimeError("Could not locate 'kubernetes' plugin in CoreDNS Corefile")

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

            self.kubectl.exec_command_checked(f"kubectl apply -f {tmp_file_path}")

            # Restart CoreDNS
            self.kubectl.exec_command_checked("kubectl -n kube-system rollout restart deployment coredns")
            self.kubectl.exec_command_checked("kubectl -n kube-system rollout status deployment coredns --timeout=30s")

            corefile_after = yaml.safe_load(
                self.kubectl.exec_command_checked("kubectl -n kube-system get cm coredns -o yaml")
            )["data"]["Corefile"]
            if start_line_id not in corefile_after:
                raise RuntimeError(f"CoreDNS did not retain the NXDOMAIN rule for {fqdn}")

            print(f"Injected Service DNS Resolution Failure fault for service: {service}")

    def recover_service_dns_resolution_failure(self, microservices: list[str]):
        for service in microservices:
            fqdn = f"{service}.{self.namespace}.svc.cluster.local"

            # Get configmap as structured data
            cm_yaml = self.kubectl.exec_command_checked("kubectl -n kube-system get cm coredns -o yaml")
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
                raise RuntimeError("CoreDNS NXDOMAIN template block was not properly closed")

            new_corefile = "\n".join(new_lines)

            # Verify if the removal worked
            if start_line_id in new_corefile:
                raise RuntimeError("CoreDNS NXDOMAIN template was not successfully removed")

            cm_data["data"]["Corefile"] = new_corefile

            # Apply using temporary file
            tmp_file_path = self._write_yaml_to_file("coredns", cm_data)
            self.kubectl.exec_command_checked(f"kubectl apply -f {tmp_file_path}")

            # Restart CoreDNS
            self.kubectl.exec_command_checked("kubectl -n kube-system rollout restart deployment coredns")
            self.kubectl.exec_command_checked("kubectl -n kube-system rollout status deployment coredns --timeout=30s")

            corefile_after = yaml.safe_load(
                self.kubectl.exec_command_checked("kubectl -n kube-system get cm coredns -o yaml")
            )["data"]["Corefile"]
            if start_line_id in corefile_after:
                raise RuntimeError(f"CoreDNS still contains the NXDOMAIN rule for {fqdn}")

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

        def _exec_or_raise(command: str, action: str):
            result = self.kubectl.exec_command(command)
            if result and "error" in result.lower():
                msg = f"{action} failed: {result.strip()}"
                print(msg)
                raise RuntimeError(msg)
            return result

        # Apply using temporary file
        tmp_file_path = self._write_yaml_to_file("coredns", cm_data)
        _exec_or_raise(f"kubectl apply -f {tmp_file_path}", "Applying CoreDNS configmap")

        # Restart CoreDNS
        _exec_or_raise("kubectl -n kube-system rollout restart deployment coredns", "Restarting CoreDNS")
        _exec_or_raise(
            "kubectl -n kube-system rollout status deployment coredns --timeout=30s",
            "Waiting for CoreDNS rollout",
        )

        # Verify stale template is gone after apply/restart
        cm_yaml_after = self.kubectl.exec_command("kubectl -n kube-system get cm coredns -o yaml")
        cm_data_after = yaml.safe_load(cm_yaml_after)
        corefile_after = cm_data_after["data"]["Corefile"]
        if template_id in corefile_after:
            msg = "CoreDNS config still contains stale NXDOMAIN template after recovery"
            print(msg)
            raise RuntimeError(msg)

        print("Recovered from stale CoreDNS config for all .svc.cluster.local domains")

    def recover_all_nxdomain_templates(self):
        """
        Remove ALL NXDOMAIN template blocks from CoreDNS config.
        This handles both:
        - stale_coredns_config: `template ANY ANY svc.cluster.local`
        - service_dns_resolution_failure: `template ANY ANY {service}.{namespace}.svc.cluster.local`
        """
        import re

        cm_yaml = self.kubectl.exec_command("kubectl -n kube-system get cm coredns -o yaml")
        cm_data = yaml.safe_load(cm_yaml)
        corefile = cm_data["data"]["Corefile"]

        # Pattern to match any NXDOMAIN template block
        # Matches: template ANY ANY <anything> { ... rcode NXDOMAIN ... }
        nxdomain_pattern = re.compile(
            r"^\s*template\s+ANY\s+ANY\s+[^\n]+\{[^}]*rcode\s+NXDOMAIN[^}]*\}\s*\n?",
            re.MULTILINE | re.DOTALL,
        )

        if not nxdomain_pattern.search(corefile):
            print("No NXDOMAIN templates found in CoreDNS config; nothing to do")
            return

        new_corefile = nxdomain_pattern.sub("", corefile)

        # Remove any resulting double blank lines
        new_corefile = re.sub(r"\n{3,}", "\n\n", new_corefile)

        cm_data["data"]["Corefile"] = new_corefile

        def _exec_or_raise(command: str, action: str):
            result = self.kubectl.exec_command(command)
            if result and "error" in result.lower():
                msg = f"{action} failed: {result.strip()}"
                print(msg)
                raise RuntimeError(msg)
            return result

        # Apply using temporary file
        tmp_file_path = self._write_yaml_to_file("coredns", cm_data)
        _exec_or_raise(f"kubectl apply -f {tmp_file_path}", "Applying CoreDNS configmap")

        # Restart CoreDNS
        _exec_or_raise("kubectl -n kube-system rollout restart deployment coredns", "Restarting CoreDNS")
        _exec_or_raise(
            "kubectl -n kube-system rollout status deployment coredns --timeout=30s",
            "Waiting for CoreDNS rollout",
        )

        # Verify all NXDOMAIN templates are gone
        cm_yaml_after = self.kubectl.exec_command("kubectl -n kube-system get cm coredns -o yaml")
        cm_data_after = yaml.safe_load(cm_yaml_after)
        corefile_after = cm_data_after["data"]["Corefile"]
        if nxdomain_pattern.search(corefile_after):
            msg = "CoreDNS config still contains NXDOMAIN templates after recovery"
            print(msg)
            raise RuntimeError(msg)

        print("Recovered all NXDOMAIN templates from CoreDNS config")

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

    # Inject a liveness probe too aggressive fault
    def inject_liveness_probe_too_aggressive(self, microservices: list[str]):
        for service in microservices:
            script_path = Path(__file__).parent / "custom" / "slow_service.py"
            self.deploy_custom_service(service, script_path)

            deployment_yaml = self._get_deployment_yaml(service)
            original_deployment_yaml = copy.deepcopy(deployment_yaml)

            containers = deployment_yaml["spec"]["template"]["spec"]["containers"]

            for container in containers:
                probe = container.get("livenessProbe")
                if probe:
                    probe["initialDelaySeconds"] = 0
                    probe["periodSeconds"] = 1
                    probe["failureThreshold"] = 1

            deployment_yaml["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] = 0

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_command = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_command)
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(apply_command)
            print(f"Apply result for {service}: {apply_result}")

            # Save the *original* deployment YAML for recovery
            self._write_yaml_to_file(service, original_deployment_yaml)

            self.kubectl.wait_for_stable(self.namespace)

            print(f"Injected liveness probe too aggressive fault for service: {service}")

    def recover_liveness_probe_too_aggressive(self, microservices: list[str]):
        for service in microservices:
            original_yaml_path = f"/tmp/{service}_modified.yaml"

            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_command = f"kubectl apply -f {original_yaml_path} -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_command)
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(apply_command)
            print(f"Apply result for {service}: {apply_result}")

            self.kubectl.wait_for_ready(self.namespace)

            print(f"Recovered from liveness probe too aggressive fault for service: {service}")

    # V.14 - Injects an environment variable leak by deleting a ConfigMap and restarting the associated deployment.
    def inject_missing_configmap(self, microservices: list[str]):
        for microservice in microservices:
            configmap_name = None
            if self.namespace == "social-network":
                configmap_name = "media-mongodb"
            elif self.namespace == "hotel-reservation":
                configmap_name = "mongo-geo-script"
            else:
                raise ValueError(f"Unknown namespace: {self.namespace}")

            get_cmd = f"kubectl get configmap {configmap_name} -n {self.namespace} -o yaml"
            original_yaml = self.kubectl.exec_command(get_cmd)
            parsed_yaml = yaml.safe_load(original_yaml)

            self._write_yaml_to_file(microservice, parsed_yaml)

            delete_cmd = f"kubectl delete configmap {configmap_name} -n {self.namespace}"
            self.kubectl.exec_command(delete_cmd)
            print(f"Deleted ConfigMap: {configmap_name}")

            # Scale down to 0 to terminate all healthy pods, then scale back up so only the faulty pod starts
            self.kubectl.exec_command(f"kubectl scale deployment {microservice} -n {self.namespace} --replicas=0")
            self.kubectl.exec_command(
                f"kubectl rollout status deployment {microservice} -n {self.namespace} --timeout=60s"
            )
            self.kubectl.exec_command(f"kubectl scale deployment {microservice} -n {self.namespace} --replicas=1")
            print("Restarted pods to apply ConfigMap fault")

    def recover_missing_configmap(self, microservices: list[str]):
        for microservice in microservices:
            configmap_name = f"{microservice}"
            backup_path = f"/tmp/{configmap_name}_modified.yaml"

            apply_cmd = f"kubectl apply -f {backup_path} -n {self.namespace}"
            self.kubectl.exec_command(apply_cmd)
            print(f"Restored ConfigMap: {configmap_name}")

            self.kubectl.exec_command(f"kubectl rollout restart deployment {microservice} -n {self.namespace}")
            self.kubectl.exec_command(f"kubectl rollout status deployment {microservice} -n {self.namespace}")
            print(f"Deployment {microservice} restarted and should now be healthy")

    # Inject ConfigMap drift by removing critical keys
    def inject_configmap_drift(self, microservices: list[str]):
        for service in microservices:
            # Read the actual config.json from the running pod
            read_config_cmd = f"kubectl exec deployment/{service} -n {self.namespace} -- cat /go/src/github.com/harlow/go-micro-services/config.json"
            config_json_str = self.kubectl.exec_command(read_config_cmd)
            original_config = json.loads(config_json_str)
            print(f"Read original config from {service} pod")

            # Save the original config to a file for recovery
            original_config_path = f"/tmp/{service}-original-config.json"
            with open(original_config_path, "w") as f:
                json.dump(original_config, f, indent=2)
            print(f"Saved original config to {original_config_path}")

            fault_config = copy.deepcopy(original_config)
            key_to_remove = None

            if service == "geo" and "GeoMongoAddress" in fault_config:
                del fault_config["GeoMongoAddress"]
                key_to_remove = "GeoMongoAddress"
            else:
                print(f"Service {service} not supported for ConfigMap drift fault")
                continue

            configmap_name = f"{service}-config"
            fault_config_json = json.dumps(fault_config, indent=2)

            create_cm_cmd = f"""kubectl create configmap {configmap_name} -n {self.namespace} --from-literal=config.json='{fault_config_json}' --dry-run=client -o yaml | kubectl apply -f -"""
            self.kubectl.exec_command(create_cm_cmd)
            print(f"Created ConfigMap {configmap_name} with {key_to_remove} removed")

            json_patch = [
                {
                    "op": "add",
                    "path": "/spec/template/spec/volumes/-",
                    "value": {"name": "config-volume", "configMap": {"name": configmap_name}},
                },
                {
                    "op": "add",
                    "path": "/spec/template/spec/containers/0/volumeMounts/-",
                    "value": {
                        "name": "config-volume",
                        "mountPath": "/go/src/github.com/harlow/go-micro-services/config.json",
                        "subPath": "config.json",
                    },
                },
            ]

            # Check if volumes array exists, if not create it
            check_volumes_cmd = (
                f"kubectl get deployment {service} -n {self.namespace} -o jsonpath='{{.spec.template.spec.volumes}}'"
            )
            volumes_exist = self.kubectl.exec_command(check_volumes_cmd).strip()

            if not volumes_exist or volumes_exist == "[]":
                # Need to create the volumes array first
                json_patch[0]["op"] = "add"
                json_patch[0]["path"] = "/spec/template/spec/volumes"
                json_patch[0]["value"] = [json_patch[0]["value"]]

            # Check if volumeMounts array exists
            check_mounts_cmd = f"kubectl get deployment {service} -n {self.namespace} -o jsonpath='{{.spec.template.spec.containers[0].volumeMounts}}'"
            mounts_exist = self.kubectl.exec_command(check_mounts_cmd).strip()

            if not mounts_exist or mounts_exist == "[]":
                # Need to create the volumeMounts array first
                json_patch[1]["op"] = "add"
                json_patch[1]["path"] = "/spec/template/spec/containers/0/volumeMounts"
                json_patch[1]["value"] = [json_patch[1]["value"]]

            patch_json_str = json.dumps(json_patch)
            patch_cmd = f"kubectl patch deployment {service} -n {self.namespace} --type='json' -p='{patch_json_str}'"
            patch_result = self.kubectl.exec_command(patch_cmd)
            print(f"Patch result for {service}: {patch_result}")

            self.kubectl.exec_command(f"kubectl rollout status deployment/{service} -n {self.namespace} --timeout=30s")

            print(f"Injected ConfigMap drift fault for service: {service} - removed {key_to_remove}")

    def recover_configmap_drift(self, microservices: list[str]):
        for service in microservices:
            # Use the same ConfigMap name as in injection
            configmap_name = f"{service}-config"

            # Read the saved original config instead of trying to read from the pod
            original_config_path = f"/tmp/{service}-original-config.json"
            with open(original_config_path) as f:
                original_config = json.load(f)
            print(f"Read original config from saved file: {original_config_path}")

            original_config_json = json.dumps(original_config, indent=2)
            update_cm_cmd = f"""kubectl create configmap {configmap_name} -n {self.namespace} --from-literal=config.json='{original_config_json}' --dry-run=client -o yaml | kubectl apply -f -"""
            self.kubectl.exec_command(update_cm_cmd)
            print(f"Updated ConfigMap {configmap_name} with complete configuration")

            self.kubectl.exec_command(f"kubectl rollout restart deployment/{service} -n {self.namespace}")
            self.kubectl.exec_command(f"kubectl rollout status deployment/{service} -n {self.namespace} --timeout=30s")

            print(f"Recovered ConfigMap drift fault for service: {service}")

    # V.14 - Inject a readiness probe misconfiguration fault
    def inject_readiness_probe_misconfiguration(self, microservices: list[str]):
        for service in microservices:
            deployment_yaml = self._get_deployment_yaml(service)
            original_deployment_yaml = copy.deepcopy(deployment_yaml)

            containers = deployment_yaml["spec"]["template"]["spec"]["containers"]

            initial_delay = 10

            for container in containers:
                container["readinessProbe"] = {
                    "httpGet": {"path": "/healthz", "port": 8080},
                    "initialDelaySeconds": initial_delay,
                    "periodSeconds": 10,
                    "failureThreshold": 1,
                }

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_command = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_command)
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(apply_command)
            print(f"Apply result for {service}: {apply_result}")

            # Save the *original* deployment YAML for recovery
            self._write_yaml_to_file(service, original_deployment_yaml)

            print(f"Injected readiness probe misconfiguration fault for service: {service}")

    def recover_readiness_probe_misconfiguration(self, microservices: list[str]):
        for service in microservices:
            original_yaml_path = f"/tmp/{service}_modified.yaml"

            delete_command = f"kubectl delete deployment {service} -n {self.namespace} --ignore-not-found=true"
            apply_command = f"kubectl apply -f {original_yaml_path} -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_command)
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(apply_command)
            print(f"Apply result for {service}: {apply_result}")

            self.kubectl.wait_for_ready(self.namespace)

            print(f"Recovered from readiness probe misconfiguration fault for service: {service}")

    # V.15 - Inject a liveness probe misconfiguration fault
    def inject_liveness_probe_misconfiguration(self, microservices: list[str]):
        for service in microservices:
            deployment_yaml = self._get_deployment_yaml(service)
            original_deployment_yaml = copy.deepcopy(deployment_yaml)

            containers = deployment_yaml["spec"]["template"]["spec"]["containers"]
            initial_delay = 10

            for container in containers:
                container["livenessProbe"] = {
                    "httpGet": {"path": "/healthz", "port": 8080},
                    "initialDelaySeconds": initial_delay,
                    "periodSeconds": 10,
                    "failureThreshold": 1,
                }

            # Set terminationGracePeriodSeconds at the pod template spec level (not inside a container spec)
            deployment_yaml["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] = 0

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_command = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_command)
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(apply_command)
            print(f"Apply result for {service}: {apply_result}")

            # Save the *original* deployment YAML for recovery
            self._write_yaml_to_file(service, original_deployment_yaml)

            print(f"Injected liveness probe misconfiguration fault for service: {service}")

    def recover_liveness_probe_misconfiguration(self, microservices: list[str]):
        for service in microservices:
            original_yaml_path = f"/tmp/{service}_modified.yaml"

            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_command = f"kubectl apply -f {original_yaml_path} -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_command)
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(apply_command)
            print(f"Apply result for {service}: {apply_result}")

            self.kubectl.wait_for_ready(self.namespace)

            print(f"Recovered from liveness probe misconfiguration fault for service: {service}")

    # Duplicate PVC mounts multiple replicas share ReadWriteOnce PVC causing mount conflict
    def _storage_baseline_path(self, service: str) -> Path:
        return Path("/tmp") / f"deployment-state-{self.namespace}-{service}.yaml"

    @staticmethod
    def _reapplicable_deployment_manifest(deployment_yaml: dict) -> dict:
        manifest = copy.deepcopy(deployment_yaml)
        manifest.pop("status", None)

        metadata = manifest.setdefault("metadata", {})
        for field in (
            "creationTimestamp",
            "generation",
            "managedFields",
            "resourceVersion",
            "selfLink",
            "uid",
        ):
            metadata.pop(field, None)

        annotations = metadata.get("annotations") or {}
        annotations.pop("deployment.kubernetes.io/revision", None)
        annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
        if annotations:
            metadata["annotations"] = annotations
        else:
            metadata.pop("annotations", None)
        return manifest

    def _save_storage_baseline(self, service: str, deployment_yaml: dict) -> Path:
        path = self._storage_baseline_path(service)
        path.write_text(yaml.safe_dump(self._reapplicable_deployment_manifest(deployment_yaml)))
        return path

    def _legacy_statefulset_claims(self, service: str) -> list[str]:
        try:
            statefulset = self.kubectl.apps_v1_api.read_namespaced_stateful_set(
                name=service,
                namespace=self.namespace,
            )
        except ApiException as exc:
            if exc.status == 404:
                return []
            raise

        prefixes = [
            f"{template.metadata.name}-{service}-" for template in statefulset.spec.volume_claim_templates or []
        ]
        if not prefixes:
            return []

        claims = self.kubectl.core_v1_api.list_namespaced_persistent_volume_claim(self.namespace)
        return [
            claim.metadata.name
            for claim in claims.items
            if any(claim.metadata.name.startswith(prefix) for prefix in prefixes)
        ]

    def inject_duplicate_pvc_mounts(self, microservices: list[str]):
        for service in microservices:
            original_deployment_yaml = self._get_deployment_yaml(service)
            deployment_yaml = copy.deepcopy(original_deployment_yaml)

            # Create a single PVC that every replica will try to use
            pvc_name = f"{service}-pvc"
            try:
                self.kubectl.core_v1_api.read_namespaced_persistent_volume_claim(
                    name=pvc_name,
                    namespace=self.namespace,
                )
            except ApiException as exc:
                if exc.status != 404:
                    raise
            else:
                raise RuntimeError(
                    f"Refusing to replace pre-existing PersistentVolumeClaim '{pvc_name}' "
                    f"in namespace '{self.namespace}'"
                )

            baseline_path = self._save_storage_baseline(service, original_deployment_yaml)
            print(f"Saved the current Deployment configuration to {baseline_path}")

            pvc_manifest = {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": pvc_name, "namespace": self.namespace},
                "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}},
            }

            pvc_json = json.dumps(pvc_manifest)
            self.kubectl.exec_command(f"kubectl apply -f - <<EOF\n{pvc_json}\nEOF")

            print(f"Created PVC {pvc_name} for fault injection")

            pod_spec = deployment_yaml.get("spec", {}).get("template", {}).get("spec", {})

            if "volumes" not in pod_spec:
                pod_spec["volumes"] = []
            pod_spec["volumes"].append(
                {
                    "name": f"{service}-volume",
                    "persistentVolumeClaim": {"claimName": pvc_name},
                }
            )

            containers = pod_spec.get("containers", [])
            if containers:
                if "volumeMounts" not in containers[0]:
                    containers[0]["volumeMounts"] = []
                containers[0]["volumeMounts"].append(
                    {
                        "name": f"{service}-volume",
                        "mountPath": f"/{service}-data",
                    }
                )

            if "affinity" not in pod_spec:
                pod_spec["affinity"] = {}

            label_key = next(iter(deployment_yaml["spec"]["selector"]["matchLabels"]))
            label_val = deployment_yaml["spec"]["selector"]["matchLabels"][label_key]

            pod_spec["affinity"]["podAntiAffinity"] = {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {
                            "matchExpressions": [{"key": label_key, "operator": "In", "values": [label_val]}]
                        },
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            }

            # Ensure at least two replicas
            deployment_yaml["spec"]["replicas"] = max(deployment_yaml["spec"].get("replicas", 1), 2)

            yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            delete_result = self.kubectl.exec_command(f"kubectl delete deployment {service} -n {self.namespace}")
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(f"kubectl apply -f {yaml_path} -n {self.namespace}")
            print(f"Apply result for {service}: {apply_result}")

            print(
                f"Injected Duplicate PVC Mounts fault for {service}: replicas={deployment_yaml['spec']['replicas']}, shared PVC={pvc_name}"
            )

    def recover_duplicate_pvc_mounts(self, microservices: list[str]):
        for service in microservices:
            baseline_path = self._storage_baseline_path(service)
            if not baseline_path.exists():
                raise RuntimeError(f"Saved Deployment configuration is missing: {baseline_path}")

            legacy_claims = self._legacy_statefulset_claims(service)
            self.kubectl.exec_command(
                f"kubectl delete statefulset {service} -n {self.namespace} "
                "--ignore-not-found=true --wait=true --timeout=120s"
            )
            self.kubectl.exec_command(
                f"kubectl delete deployment {service} -n {self.namespace} "
                "--ignore-not-found=true --wait=true --timeout=120s"
            )

            injected_claims = [f"{service}-pvc", *legacy_claims]
            for claim_name in dict.fromkeys(injected_claims):
                self.kubectl.exec_command(
                    f"kubectl delete pvc {claim_name} -n {self.namespace} "
                    "--ignore-not-found=true --wait=true --timeout=120s"
                )

            remaining_claims = {
                claim.metadata.name
                for claim in self.kubectl.core_v1_api.list_namespaced_persistent_volume_claim(self.namespace).items
            }
            not_deleted = remaining_claims.intersection(injected_claims)
            if not_deleted:
                raise RuntimeError(f"Could not remove injected storage claims: {sorted(not_deleted)}")

            apply_result = self.kubectl.exec_command(f"kubectl apply -f {baseline_path} -n {self.namespace}")
            print(f"Restore result for {service}: {apply_result}")
            self.kubectl.get_deployment(service, self.namespace)
            self.kubectl.exec_command(f"kubectl rollout status deployment/{service} -n {self.namespace} --timeout=120s")
            self.kubectl.wait_for_ready(
                self.namespace,
                service_names=service,
                max_wait=180,
            )
            baseline_path.unlink()

            print(f"Restored the original Deployment and removed injected storage for {service}")

    # Inject environment variable shadowing fault
    def inject_env_variable_shadowing(self, microservices: list[str]):
        for service in microservices:
            deployment_yaml = self._get_deployment_yaml(service)
            original_deployment_yaml = copy.deepcopy(deployment_yaml)

            containers = deployment_yaml["spec"]["template"]["spec"]["containers"]

            shadow_vars = None

            if self.namespace == "astronomy-shop":
                if service == "frontend-proxy":
                    shadow_vars = {"FRONTEND_HOST": "localhost"}

            for container in containers:
                if "env" not in container:
                    container["env"] = []

                for env_var, value in shadow_vars.items():
                    env_exists = False
                    for existing_env in container["env"]:
                        if existing_env.get("name") == env_var:
                            existing_env["value"] = value
                            env_exists = True
                            break

                    if not env_exists:
                        container["env"].append({"name": env_var, "value": value})

                print(
                    f"Added shadowing environment variables to container {container.get('name', 'unnamed')}: {list(shadow_vars.keys())}"
                )

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_command = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_command)
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(apply_command)
            print(f"Apply result for {service}: {apply_result}")

            # Save the *original* deployment YAML for recovery
            self._write_yaml_to_file(service, original_deployment_yaml)

            print(f"Injected environment variable shadowing fault for service: {service}")

    def recover_env_variable_shadowing(self, microservices: list[str]):
        for service in microservices:
            original_yaml_path = f"/tmp/{service}_modified.yaml"

            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_command = f"kubectl apply -f {original_yaml_path} -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_command)
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(apply_command)
            print(f"Apply result for {service}: {apply_result}")

            self.kubectl.exec_command(f"kubectl rollout restart deployment/load-generator -n {self.namespace}")
            self.kubectl.exec_command(
                f"kubectl rollout status deployment/load-generator -n {self.namespace} --timeout=60s"
            )

            self.kubectl.wait_for_ready(self.namespace)

            print(f"Recovered from environment variable shadowing fault for service: {service}")

    def _wait_for_deployment_rollout(self, service: str, timeout: int = 120, sleep: int = 2) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            deployment = self.kubectl.apps_v1_api.read_namespaced_deployment(service, self.namespace)
            desired = deployment.spec.replicas
            if desired is None:
                desired = 1

            status = deployment.status
            generation = deployment.metadata.generation or 0
            if (
                desired > 0
                and (status.observed_generation or 0) >= generation
                and (status.updated_replicas or 0) == desired
                and (status.ready_replicas or 0) == desired
                and (status.available_replicas or 0) == desired
                and (status.unavailable_replicas or 0) == 0
            ):
                return

            time.sleep(sleep)

        raise TimeoutError(f"Deployment '{service}' did not complete its healthy baseline rollout within {timeout}s")

    # Inject Rolling Update Misconfiguration
    def inject_rolling_update_misconfigured(self, microservices: list[str]):
        import tempfile

        for service in microservices:
            base_dep = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": service,
                    "namespace": self.namespace,
                    "labels": {"app": service},
                },
                "spec": {
                    "replicas": 3,
                    "selector": {"matchLabels": {"app": service}},
                    "template": {
                        "metadata": {"labels": {"app": service}},
                        "spec": {
                            "containers": [
                                {
                                    "name": f"{service}-main",
                                    "image": "python:3.9-slim",
                                    "command": ["python3", "-m", "http.server", "8080"],
                                    "ports": [{"containerPort": 8080}],
                                }
                            ]
                        },
                    },
                },
            }
            print(f"➡️ Deploying {service}")
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
                yaml.safe_dump(base_dep, tmp)
                path0 = tmp.name
            self.kubectl.exec_command(f"kubectl apply -f {path0} -n {self.namespace}")
            self._wait_for_deployment_rollout(service)
            print(f"Healthy baseline rollout completed for `{service}`")

            orig_path = f"/tmp/{service}-orig.yaml"
            with open(orig_path, "w") as f:
                yaml.safe_dump(base_dep, f)

            dep = copy.deepcopy(base_dep)
            dep["spec"]["strategy"] = {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": "100%", "maxSurge": "0%"},
            }
            init = {
                "name": "hang-init",
                "image": "busybox:1.36",
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-c", "sleep infinity"],
            }
            dep.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {}).setdefault(
                "initContainers", []
            ).append(init)

            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp2:
                yaml.safe_dump(dep, tmp2)
                path1 = tmp2.name

            self.kubectl.exec_command(f"kubectl patch deployment {service} -n {self.namespace} --patch-file {path1}")
            self.kubectl.exec_command(f"kubectl rollout restart deployment {service} -n {self.namespace}")
            print(f"⚠️ Injected Rolling Update Misconfiguration fault into `{service}`")

    def recover_rolling_update_misconfigured(self, microservices: list[str]):
        for service in microservices:
            original_yaml_path = f"/tmp/{service}-orig.yaml"

            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            delete_result = self.kubectl.exec_command(delete_command)
            print(f"Deleted faulty deployment {service}: {delete_result}")

            apply_command = f"kubectl apply -f {original_yaml_path} -n {self.namespace}"
            apply_result = self.kubectl.exec_command(apply_command)
            print(f"Restored original deployment {service}: {apply_result}")

            self.kubectl.exec_command(f"kubectl rollout status deployment/{service} -n {self.namespace} --timeout=120s")

    def deploy_custom_service(self, service_name: str, script_path: str):
        print(f"Deploying {service_name} Service...................................")
        import tempfile

        import yaml

        with open(script_path) as sf:
            script_body = sf.read()

        script_filename = "service.py"

        configmap = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"{service_name}-script",
                "namespace": self.namespace,
            },
            "data": {script_filename: script_body},
        }

        self.kubectl.exec_command(f"kubectl apply -f - <<'CM'\n{yaml.dump(configmap)}\nCM")

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": service_name,
                "namespace": self.namespace,
                "labels": {"app": service_name},
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": service_name}},
                "template": {
                    "metadata": {"labels": {"app": service_name}},
                    "spec": {
                        "containers": [
                            {
                                "name": f"{service_name}-container",
                                "image": "python:3.9-slim",
                                "command": ["python", "/app/service.py"],
                                "ports": [{"containerPort": 8080, "name": "http"}],
                                "volumeMounts": [
                                    {
                                        "name": "script-vol",
                                        "mountPath": "/app/service.py",
                                        "subPath": "service.py",
                                    }
                                ],
                                "livenessProbe": {
                                    "httpGet": {"path": "/health", "port": 8080},
                                    "initialDelaySeconds": 60,
                                    "periodSeconds": 10,
                                    "failureThreshold": 3,
                                },
                                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}},
                            }
                        ],
                        "volumes": [
                            {
                                "name": "script-vol",
                                "configMap": {"name": f"{service_name}-script"},
                            }
                        ],
                    },
                },
            },
        }

        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": service_name,
                "namespace": self.namespace,
                "labels": {"app": service_name},
            },
            "spec": {
                "selector": {"app": service_name},
                "ports": [
                    {
                        "port": 8080,
                        "targetPort": 8080,
                        "protocol": "TCP",
                        "name": "http",
                    }
                ],
                "type": "ClusterIP",
            },
        }

        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".yaml") as tmp:
            yaml.dump_all([deployment, service], tmp)
            tmp_path = tmp.name

        self.kubectl.exec_command(f"kubectl apply -f {tmp_path}")
        self.kubectl.wait_for_ready(namespace=self.namespace, service_names=service_name)

        print(f"Deployed {service_name} Service...................................")

    def inject_toleration_without_matching_taint(
        self,
        microservices: list[str],
        node_name: str,
        taint_key: str = "sre-fault",
        taint_value: str = "blocked",
        effect: str = "NoSchedule",
    ):
        self.kubectl.exec_command(f"kubectl taint node {node_name} {taint_key}={taint_value}:{effect} --overwrite")
        print(f"Tainted node {node_name} with {taint_key}={taint_value}:{effect}")

        for svc in microservices:
            self.kubectl.exec_command(f"kubectl delete pod -l app={svc} -n {self.namespace}")
        print(f"Deleted pods for {microservices}; they should now be unschedulable.")

    def recover_toleration_without_matching_taint(
        self,
        microservices: list[str],
        node_name: str,
        taint_key: str = "sre-fault",
        taint_value: str = "blocked",
        effect: str = "NoSchedule",
    ):
        self.kubectl.exec_command(f"kubectl taint node {node_name} {taint_key}={taint_value}:{effect}-")
        print(f"Removed taint from node {node_name}")

        for svc in microservices:
            self.kubectl.exec_command(f"kubectl rollout restart deployment {svc} -n {self.namespace}")
        self.kubectl.wait_for_stable(self.namespace)
        print(f"Pods for {microservices} are back to Running")

    def inject_persistent_volume_affinity_violation(self, microservices: list[str]):
        nodes = [
            node.metadata.name
            for node in self.kubectl.list_nodes().items
            if "node-role.kubernetes.io/control-plane" not in node.metadata.labels
        ]

        if len(nodes) < 2:
            raise RuntimeError("Need 2 worker nodes for this fault to be injected.")

        nodeA, nodeB = nodes[0], nodes[1]

        for service in microservices:
            deployment_yaml = self._get_deployment_yaml(service)
            original_deployment_yaml = copy.deepcopy(deployment_yaml)

            # Create a PV that's bound to node A
            pv_manifest = {
                "apiVersion": "v1",
                "kind": "PersistentVolume",
                "metadata": {"name": "temp-pv"},
                "spec": {
                    "capacity": {"storage": "1Gi"},
                    "accessModes": ["ReadWriteOnce"],
                    "persistentVolumeReclaimPolicy": "Delete",
                    "storageClassName": "",
                    "hostPath": {"path": "/tmp/data/volumes/temp-pv"},
                    "nodeAffinity": {
                        "required": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": "kubernetes.io/hostname",
                                            "operator": "In",
                                            "values": [nodeA],
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                    "claimRef": {"name": "temp-pvc", "namespace": self.namespace},
                },
            }

            pv_json = json.dumps(pv_manifest)
            self.kubectl.exec_command(f"kubectl apply -f - <<EOF\n{pv_json}\nEOF")
            print("Created PV temp-pv for fault injection")

            # Create a PVC bound to the PV above
            pvc_manifest = {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "temp-pvc", "namespace": self.namespace},
                "spec": {
                    "storageClassName": "",
                    "volumeName": "temp-pv",
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "1Gi"}},
                },
            }

            pvc_json = json.dumps(pvc_manifest)
            self.kubectl.exec_command(f"kubectl apply -f - <<EOF\n{pvc_json}\nEOF")
            print("Created PVC temp-pvc for fault injection")

            pod_spec = deployment_yaml.get("spec", {}).get("template", {}).get("spec", {})

            self._change_node_selector(deployment_yaml=deployment_yaml, node_name=nodeB)

            if "volumes" not in pod_spec:
                pod_spec["volumes"] = []
            pod_spec["volumes"].append(
                {
                    "name": f"{service}-volume",
                    "persistentVolumeClaim": {"claimName": "temp-pvc"},
                }
            )

            containers = pod_spec.get("containers", [])
            if containers:
                if "volumeMounts" not in containers[0]:
                    containers[0]["volumeMounts"] = []
                containers[0]["volumeMounts"].append(
                    {
                        "name": f"{service}-volume",
                        "mountPath": f"/{service}-data",
                    }
                )

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            delete_result = self.kubectl.exec_command(f"kubectl delete deployment {service} -n {self.namespace}")
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(f"kubectl apply -f {modified_yaml_path} -n {self.namespace}")
            print(f"Apply result for {service}: {apply_result}")

            self._write_yaml_to_file(service, original_deployment_yaml)

            print(f"Injected persistent volume affinity conflict fault for {service}")

    def recover_persistent_volume_affinity_violation(self, microservices: list[str]):
        for service in microservices:
            original_yaml_path = f"/tmp/{service}_modified.yaml"

            delete_command = f"kubectl delete --ignore-not-found=true deployment {service} -n {self.namespace}"
            delete_pv_command = "kubectl delete --ignore-not-found=true pv temp-pv"
            delete_pvc_command = f"kubectl delete --ignore-not-found=true pvc temp-pvc -n {self.namespace}"
            apply_command = f"kubectl apply -f {original_yaml_path} -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_command)
            print(f"Delete result for {service}: {delete_result}")

            delete_pvc_result = self.kubectl.exec_command(delete_pvc_command)
            print(f"Delete PVC result: {delete_pvc_result}")

            delete_pv_result = self.kubectl.exec_command(delete_pv_command)
            print(f"Delete PV result: {delete_pv_result}")

            apply_result = self.kubectl.exec_command(apply_command)
            print(f"Apply result for {service}: {apply_result}")

            self.kubectl.wait_for_ready(self.namespace)

            print(f"Recovered from persistent volume affinity violation fault for service: {service}")

    def inject_pod_anti_affinity_deadlock(self, microservices: list[str]):
        """
        Inject a fault that creates pod anti-affinity deadlock.
        Sets requiredDuringScheduling anti-affinity that excludes all nodes.
        """
        for service in microservices:
            deployment_yaml = self._get_deployment_yaml(service)

            # Set replicas higher than node count to guarantee a scheduling deadlock.
            node_count = int(self.kubectl.exec_command("kubectl get nodes --no-headers | wc -l").strip())
            deployment_yaml["spec"]["replicas"] = node_count + 1

            # Create anti-affinity rules that prevent pods from being scheduled on same nodes
            anti_affinity_rules = {
                "podAntiAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "labelSelector": {
                                "matchExpressions": [{"key": "app", "operator": "In", "values": [service]}]
                            },
                            "topologyKey": "kubernetes.io/hostname",
                        }
                    ]
                }
            }

            # Add affinity to deployment spec
            if "affinity" not in deployment_yaml["spec"]["template"]["spec"]:
                deployment_yaml["spec"]["template"]["spec"]["affinity"] = {}

            deployment_yaml["spec"]["template"]["spec"]["affinity"].update(anti_affinity_rules)

            # Write the modified YAML to a temporary file
            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            # Delete and redeploy with anti-affinity rules
            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            self.kubectl.exec_command(delete_command)

            apply_command = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"
            self.kubectl.exec_command(apply_command)

            print(f"Injected pod anti-affinity deadlock for service: {service}")
            print(f"  - Set replicas to {deployment_yaml['spec']['replicas']}")
            print("  - Added strict anti-affinity rules")

    def recover_pod_anti_affinity_deadlock(self, microservices: list[str]):
        """
        Recover from pod anti-affinity deadlock by removing anti-affinity rules.
        """
        for service in microservices:
            deployment_yaml = self._get_deployment_yaml(service)

            # Remove affinity rules
            if "affinity" in deployment_yaml["spec"]["template"]["spec"]:
                if "podAntiAffinity" in deployment_yaml["spec"]["template"]["spec"]["affinity"]:
                    del deployment_yaml["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"]

                # If affinity is now empty, remove it entirely
                if not deployment_yaml["spec"]["template"]["spec"]["affinity"]:
                    del deployment_yaml["spec"]["template"]["spec"]["affinity"]

            # Reset replicas to 1 for recovery
            deployment_yaml["spec"]["replicas"] = 1

            # Write the modified YAML to a temporary file
            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            # Delete and redeploy without anti-affinity rules
            delete_command = f"kubectl delete deployment {service} -n {self.namespace}"
            self.kubectl.exec_command(delete_command)

            apply_command = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"
            self.kubectl.exec_command(apply_command)

            print(f"Recovered pod anti-affinity deadlock for service: {service}")
            print("  - Removed anti-affinity rules")
            print("  - Reset replicas to 1")

    def inject_rpc_timeout_retries_misconfiguration(self, configmap: str):
        GRPC_CLIENT_TIMEOUT = "50ms"
        GRPC_CLIENT_RETRIES_ON_ERROR = "30"
        config_patch_command = f'kubectl patch configmap {configmap} -n {self.namespace} -p \'{{"data":{{"GRPC_CLIENT_TIMEOUT":"{GRPC_CLIENT_TIMEOUT}","GRPC_CLIENT_RETRIES_ON_ERROR":"{GRPC_CLIENT_RETRIES_ON_ERROR}"}}}}\''
        self.kubectl.exec_command(config_patch_command)
        deployment_rollout_command = f"kubectl rollout restart deployment -l configmap={configmap} -n {self.namespace}"
        self.kubectl.exec_command(deployment_rollout_command)
        self.kubectl.wait_for_ready(self.namespace)

    def recover_rpc_timeout_retries_misconfiguration(self, configmap: str):
        GRPC_CLIENT_TIMEOUT = "1s"
        GRPC_CLIENT_RETRIES_ON_ERROR = "1"
        config_patch_command = f'kubectl patch configmap {configmap} -n {self.namespace} -p \'{{"data":{{"GRPC_CLIENT_TIMEOUT":"{GRPC_CLIENT_TIMEOUT}","GRPC_CLIENT_RETRIES_ON_ERROR":"{GRPC_CLIENT_RETRIES_ON_ERROR}"}}}}\''
        self.kubectl.exec_command(config_patch_command)
        deployment_rollout_command = f"kubectl rollout restart deployment -l configmap={configmap} -n {self.namespace}"
        self.kubectl.exec_command(deployment_rollout_command)
        self.kubectl.wait_for_ready(self.namespace)

    def inject_daemon_set_image_replacement(self, daemon_set_name: str, new_image: str):
        daemon_set_yaml = self._get_daemon_set_yaml(daemon_set_name)
        if daemon_set_yaml is None:
            raise RuntimeError(f"Failed to get daemonset '{daemon_set_name}'")

        # Replace the image in all containers
        if "spec" in daemon_set_yaml and "template" in daemon_set_yaml["spec"]:
            template_spec = daemon_set_yaml["spec"]["template"]["spec"]
            if "containers" in template_spec:
                for container in template_spec["containers"]:
                    if "image" in container:
                        container["image"] = new_image

        modified_yaml_path = self._write_yaml_to_file(daemon_set_name, daemon_set_yaml)  # backup the yaml

        self.kubectl.exec_command(f"kubectl apply -f {modified_yaml_path}")
        self.kubectl.exec_command(f"kubectl rollout restart ds {daemon_set_name} -n {self.namespace}")
        self.kubectl.exec_command(f"kubectl rollout status ds {daemon_set_name} -n {self.namespace} --timeout=60s")

    def recover_daemon_set_image_replacement(self, daemon_set_name: str, original_image: str):
        daemon_set_yaml = self._get_daemon_set_yaml(daemon_set_name)
        if daemon_set_yaml is None:
            return
        if "spec" in daemon_set_yaml and "template" in daemon_set_yaml["spec"]:
            template_spec = daemon_set_yaml["spec"]["template"]["spec"]
            if "containers" in template_spec:
                for container in template_spec["containers"]:
                    if "image" in container and container["image"] != original_image:
                        container["image"] = original_image
                        modified_yaml_path = self._write_yaml_to_file(daemon_set_name, daemon_set_yaml)
                        self.kubectl.exec_command(f"kubectl apply -f {modified_yaml_path}")
                        self.kubectl.exec_command(f"kubectl rollout restart ds {daemon_set_name} -n {self.namespace}")
                        self.kubectl.exec_command(
                            f"kubectl rollout status ds {daemon_set_name} -n {self.namespace} --timeout=60s"
                        )
                        return

    def inject_rbac_misconfiguration(self, microservices: list[str]):
        for service in microservices:
            configmap_manifest = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "app-routing-config", "namespace": self.namespace},
                "data": {"routes.json": '{"enabled": true, "version": "1.0"}'},
            }

            cm_json = json.dumps(configmap_manifest)
            self.kubectl.exec_command(f"kubectl apply -f - <<EOF\n{cm_json}\nEOF")

            sa_manifest = {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": f"{service}-rbac-sa", "namespace": self.namespace},
            }

            sa_json = json.dumps(sa_manifest)
            self.kubectl.exec_command(f"kubectl apply -f - <<EOF\n{sa_json}\nEOF")

            # ClusterRole WITHOUT configmaps permission
            clusterrole_manifest = {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": f"{service}-rbac-role"},
                "rules": [{"apiGroups": [""], "resources": ["pods", "services"], "verbs": ["get", "list", "watch"]}],
            }

            cr_json = json.dumps(clusterrole_manifest)
            self.kubectl.exec_command(f"kubectl apply -f - <<EOF\n{cr_json}\nEOF")

            clusterrolebinding_manifest = {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": f"{service}-rbac-binding"},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": f"{service}-rbac-role",
                },
                "subjects": [{"kind": "ServiceAccount", "name": f"{service}-rbac-sa", "namespace": self.namespace}],
            }

            crb_json = json.dumps(clusterrolebinding_manifest)
            self.kubectl.exec_command(f"kubectl apply -f - <<EOF\n{crb_json}\nEOF")

            deployment_yaml = self._get_deployment_yaml(service)
            original_deployment_yaml = copy.deepcopy(deployment_yaml)
            self._write_yaml_to_file(f"{service}-original", original_deployment_yaml)

            deployment_yaml["spec"]["template"]["spec"]["serviceAccountName"] = f"{service}-rbac-sa"

            init_container = {
                "name": "config-loader",
                "image": "alpine/k8s:1.28.3",
                "command": ["kubectl", "get", "configmap", "app-routing-config", "-n", self.namespace, "-o", "json"],
            }

            if "initContainers" not in deployment_yaml["spec"]["template"]["spec"]:
                deployment_yaml["spec"]["template"]["spec"]["initContainers"] = []

            deployment_yaml["spec"]["template"]["spec"]["initContainers"].append(init_container)

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)
            self.kubectl.exec_command(f"kubectl delete deployment {service} -n {self.namespace}")
            self.kubectl.exec_command(f"kubectl apply -f {modified_yaml_path} -n {self.namespace}")

            print(f"RBAC fault injected on {service}")

    def recover_rbac_misconfiguration(self, microservices: list[str]):
        for service in microservices:
            original_yaml_path = f"/tmp/{service}-original_modified.yaml"

            self.kubectl.exec_command(
                f"kubectl delete deployment {service} -n {self.namespace} --ignore-not-found=true"
            )
            self.kubectl.exec_command(f"kubectl apply -f {original_yaml_path} -n {self.namespace}")
            self.kubectl.exec_command(
                f"kubectl delete clusterrolebinding {service}-rbac-binding --ignore-not-found=true"
            )
            self.kubectl.exec_command(f"kubectl delete clusterrole {service}-rbac-role --ignore-not-found=true")
            self.kubectl.exec_command(
                f"kubectl delete serviceaccount {service}-rbac-sa -n {self.namespace} --ignore-not-found=true"
            )
            self.kubectl.exec_command(
                f"kubectl delete configmap app-routing-config -n {self.namespace} --ignore-not-found=true"
            )

            print(f"RBAC fault recovered for {service}")

        self.kubectl.wait_for_ready(self.namespace)

    def inject_gogc_env_variable_patch(self, gogc_value: str):
        """Set GOGC environment variable for all deployments via patch method"""
        # Get all deployment names
        deployments_cmd = f"kubectl get deployments -n {self.namespace} -o jsonpath='{{.items[*].metadata.name}}'"
        deployment_names = self.kubectl.exec_command(deployments_cmd).split()

        for deployment_name in deployment_names:
            if not deployment_name:
                continue

            print(f"Patching GOGC={gogc_value} for deployment: {deployment_name}")

            # Construct patch operations
            patch_ops = []

            # Get container count
            containers_cmd = f"kubectl get deployment {deployment_name} -n {self.namespace} -o jsonpath='{{.spec.template.spec.containers[*].name}}'"
            container_names = self.kubectl.exec_command(containers_cmd).split()

            for i, _container_name in enumerate(container_names):
                # Check if env array exists
                env_check_cmd = f"kubectl get deployment {deployment_name} -n {self.namespace} -o jsonpath='{{.spec.template.spec.containers[{i}].env}}'"
                existing_env = self.kubectl.exec_command(env_check_cmd).strip()

                if not existing_env or existing_env == "[]":
                    # Create env array
                    patch_ops.append(
                        {
                            "op": "add",
                            "path": f"/spec/template/spec/containers/{i}/env",
                            "value": [{"name": "GOGC", "value": gogc_value}],
                        }
                    )
                else:
                    # Check if GOGC already exists
                    gogc_check_cmd = f"kubectl get deployment {deployment_name} -n {self.namespace} -o jsonpath='{{.spec.template.spec.containers[{i}].env[?(@.name==\"GOGC\")].value}}'"
                    existing_gogc = self.kubectl.exec_command(gogc_check_cmd).strip()

                    if existing_gogc:
                        # Update existing GOGC value
                        # Need to find GOGC's index in env array
                        env_names_cmd = f"kubectl get deployment {deployment_name} -n {self.namespace} -o jsonpath='{{.spec.template.spec.containers[{i}].env[*].name}}'"
                        env_names = self.kubectl.exec_command(env_names_cmd).split()

                        for j, env_name in enumerate(env_names):
                            if env_name == "GOGC":
                                patch_ops.append(
                                    {
                                        "op": "replace",
                                        "path": f"/spec/template/spec/containers/{i}/env/{j}/value",
                                        "value": gogc_value,
                                    }
                                )
                                break
                    else:
                        # Add new GOGC environment variable
                        patch_ops.append(
                            {
                                "op": "add",
                                "path": f"/spec/template/spec/containers/{i}/env/-",
                                "value": {"name": "GOGC", "value": gogc_value},
                            }
                        )

            if patch_ops:
                import json

                patch_json = json.dumps(patch_ops)
                patch_cmd = (
                    f"kubectl patch deployment {deployment_name} -n {self.namespace} --type='json' -p='{patch_json}'"
                )

                try:
                    result = self.kubectl.exec_command(patch_cmd)
                    print(f"Patch result for {deployment_name}: {result}")

                    # Restart deployment to apply changes
                    self.kubectl.exec_command(
                        f"kubectl rollout restart deployment {deployment_name} -n {self.namespace}"
                    )

                except Exception as e:
                    raise RuntimeError(f"Failed to patch {deployment_name}: {e}") from e

        # Wait for all deployments to be ready
        print("Waiting for all deployments to be ready...")
        for deployment_name in deployment_names:
            if deployment_name:
                self.kubectl.exec_command(
                    f"kubectl rollout status deployment {deployment_name} -n {self.namespace} --timeout=120s"
                )

        print(f"All deployments updated with GOGC={gogc_value}")

    def recover_gogc_env_variable_patch(self):
        """Recover all deployment GOGC environment variables to default value 100"""
        # Get all deployment names
        deployments_cmd = f"kubectl get deployments -n {self.namespace} -o jsonpath='{{.items[*].metadata.name}}'"
        deployment_names = self.kubectl.exec_command(deployments_cmd).split()

        for deployment_name in deployment_names:
            if not deployment_name:
                continue

            print(f"Recovering GOGC to default (100) for deployment: {deployment_name}")

            # Construct patch operations
            patch_ops = []

            # Get container count
            containers_cmd = f"kubectl get deployment {deployment_name} -n {self.namespace} -o jsonpath='{{.spec.template.spec.containers[*].name}}'"
            container_names = self.kubectl.exec_command(containers_cmd).split()

            for i, container_name in enumerate(container_names):
                # Check if env array exists
                env_check_cmd = f"kubectl get deployment {deployment_name} -n {self.namespace} -o jsonpath='{{.spec.template.spec.containers[{i}].env}}'"
                existing_env = self.kubectl.exec_command(env_check_cmd).strip()

                if existing_env and existing_env != "[]":
                    # Check if GOGC exists
                    gogc_check_cmd = f"kubectl get deployment {deployment_name} -n {self.namespace} -o jsonpath='{{.spec.template.spec.containers[{i}].env[?(@.name==\"GOGC\")].value}}'"
                    existing_gogc = self.kubectl.exec_command(gogc_check_cmd).strip()

                    if existing_gogc:
                        # Find GOGC's index in env array and update to 100
                        env_names_cmd = f"kubectl get deployment {deployment_name} -n {self.namespace} -o jsonpath='{{.spec.template.spec.containers[{i}].env[*].name}}'"
                        env_names = self.kubectl.exec_command(env_names_cmd).split()

                        for j, env_name in enumerate(env_names):
                            if env_name == "GOGC":
                                patch_ops.append(
                                    {
                                        "op": "replace",
                                        "path": f"/spec/template/spec/containers/{i}/env/{j}/value",
                                        "value": "100",
                                    }
                                )
                                print(f"Found GOGC={existing_gogc} in container {container_name}, updating to 100")
                                break
                    else:
                        print(f"No GOGC environment variable found in container {container_name}")
                else:
                    print(f"No environment variables found in container {container_name}")

            if patch_ops:
                import json

                patch_json = json.dumps(patch_ops)
                patch_cmd = (
                    f"kubectl patch deployment {deployment_name} -n {self.namespace} --type='json' -p='{patch_json}'"
                )

                try:
                    result = self.kubectl.exec_command(patch_cmd)
                    print(f"Patch result for {deployment_name}: {result}")

                    # Restart deployment to apply changes
                    self.kubectl.exec_command(
                        f"kubectl rollout restart deployment {deployment_name} -n {self.namespace}"
                    )

                except Exception as e:
                    raise RuntimeError(f"Failed to patch {deployment_name}: {e}") from e
            else:
                print(f"No GOGC environment variables to recover in deployment: {deployment_name}")

        # Wait for all deployments to be ready
        print("Waiting for all deployments to be ready...")
        for deployment_name in deployment_names:
            if deployment_name:
                try:
                    self.kubectl.exec_command(
                        f"kubectl rollout status deployment {deployment_name} -n {self.namespace} --timeout=120s"
                    )
                except Exception as e:
                    print(f"Warning: Failed to wait for deployment {deployment_name}: {e}")

        print("All deployments with GOGC environment variables have been recovered to default value (100)")

    # Inject a hostPort conflict that clashes with another service
    def inject_service_port_conflict(self, microservices: list[str], conflicting_port: int):
        for service in microservices:
            original_deployment_yaml = self._get_deployment_yaml(service)
            deployment_yaml = copy.deepcopy(original_deployment_yaml)

            containers = deployment_yaml["spec"]["template"]["spec"]["containers"]
            main_container = containers[0] if containers else {}

            # Add hostPort to the first container port (or add port if none exists)
            ports_list = main_container.get("ports", [])
            if ports_list:
                # Add hostPort to existing port
                ports_list[0]["hostPort"] = conflicting_port
            else:
                # Create a new port entry with hostPort
                main_container["ports"] = [{"containerPort": 8080, "hostPort": conflicting_port}]

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            delete_cmd = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_cmd = f"kubectl apply -f {modified_yaml_path} -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_cmd)
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(apply_cmd)
            print(f"Apply result for {service}: {apply_result}")

            # Save the *original* deployment YAML for recovery
            self._write_yaml_to_file(service, original_deployment_yaml)

            print(f"Injected hostPort {conflicting_port} conflict for service: {service}")

    def recover_service_port_conflict(self, microservices: list[str]):
        for service in microservices:
            delete_cmd = f"kubectl delete deployment {service} -n {self.namespace}"
            apply_cmd = f"kubectl apply -f /tmp/{service}_modified.yaml -n {self.namespace}"

            delete_result = self.kubectl.exec_command(delete_cmd)
            print(f"Delete result for {service}: {delete_result}")

            apply_result = self.kubectl.exec_command(apply_cmd)
            print(f"Apply result for {service}: {apply_result}")

            self.kubectl.wait_for_ready(self.namespace)

            print(f"Recovered from hostPort conflict for service: {service}")

    def inject_tor_network_partition(self, microservices: list[str]):
        """Inject a network partition using NetworkChaos."""
        chaos_resource_name = "network-segment-policy"
        tor_node_label_key = "network-segment"
        tor_pod_group_label_key = "network-segment"

        faulty_group = "segment-a"
        healthy_group = "segment-b"

        if not microservices:
            raise ValueError("inject_tor_network_partition requires a non-empty `microservices` list (faulty group).")

        # Check ChaosMesh is installed (fail-fast if not)
        crd_check = self.kubectl.exec_command("kubectl get crd networkchaos.chaos-mesh.org")
        if "NotFound" in crd_check or "Error" in crd_check:
            raise RuntimeError(
                "ChaosMesh NetworkChaos CRD not found. Install Chaos Mesh before running tor_network_partition."
            )

        # If the chaos object already exists, recover first to avoid double-injections
        existing = self.kubectl.exec_command(
            f"kubectl get networkchaos {chaos_resource_name} -n {self.namespace} -o name --ignore-not-found=true"
        ).strip()
        if existing:
            print("NetworkChaos already instantiated, recovering from previous injection.")
            self.recover_tor_network_partition(microservices)

            existing = self.kubectl.exec_command(
                f"kubectl get networkchaos {chaos_resource_name} -n {self.namespace} -o name --ignore-not-found=true"
            ).strip()
            if existing:
                raise RuntimeError("Previous NetworkChaos still present after recovery attempt.")

        # Prepare nodes (require >=2 workers)
        nodes = [
            n.metadata.name
            for n in self.kubectl.list_nodes().items
            if "node-role.kubernetes.io/control-planee" not in (n.metadata.labels or {})
            and "node-role.kubernetes.io/master" not in (n.metadata.labels or {})
        ]
        if len(nodes) < 2:
            raise RuntimeError("Top-of-rack partition requires >=2 worker nodes.")

        faulty_node, healthy_node = nodes[0], nodes[1]
        print(f"Selected faulty node: {faulty_node}")
        print(f"Selected healthy node: {healthy_node}")

        self.kubectl.exec_command(f"kubectl label node {faulty_node} {tor_node_label_key}={faulty_group} --overwrite")
        self.kubectl.exec_command(f"kubectl label node {healthy_node} {tor_node_label_key}={healthy_group} --overwrite")
        for n in nodes[2:]:
            self.kubectl.exec_command(f"kubectl label node {n} {tor_node_label_key}={healthy_group} --overwrite")

        dep_names = self.kubectl.exec_command(
            f"kubectl get deployments -n {self.namespace} -o jsonpath='{{.items[*].metadata.name}}'"
        ).split()
        dep_names = [d for d in dep_names if d]

        if not dep_names:
            raise RuntimeError(f"No deployments found in namespace {self.namespace}; is the app deployed?")

        # Force deployments onto specific node groups and pods
        for dep in dep_names:
            dep_yaml = self._get_deployment_yaml(dep)
            group = faulty_group if dep in microservices else healthy_group

            dep_yaml.setdefault("spec", {}).setdefault("template", {}).setdefault("metadata", {}).setdefault(
                "labels", {}
            )
            dep_yaml["spec"]["template"]["metadata"]["labels"][tor_pod_group_label_key] = group

            dep_yaml.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {}).setdefault(
                "nodeSelector", {}
            )
            dep_yaml["spec"]["template"]["spec"]["nodeSelector"][tor_node_label_key] = group

            modified_yaml_path = self._write_yaml_to_file(dep, dep_yaml)

            self.kubectl.exec_command(f"kubectl delete deployment {dep} -n {self.namespace}")
            self.kubectl.exec_command(f"kubectl apply -f {modified_yaml_path} -n {self.namespace}")

            print(
                f"[{dep}] Forced placement: nodeSelector {tor_node_label_key}={group}; "
                f"pod label {tor_pod_group_label_key}={group}"
            )

        self.kubectl.wait_for_stable(self.namespace)

        # Apply NetworkChaos faulty/healthy partition
        networkchaos_manifest = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "NetworkChaos",
            "metadata": {"name": chaos_resource_name, "namespace": self.namespace},
            "spec": {
                "action": "partition",
                "mode": "all",
                "direction": "both",
                "selector": {
                    "namespaces": [self.namespace],
                    "labelSelectors": {tor_pod_group_label_key: faulty_group},
                },
                "target": {
                    "mode": "all",
                    "selector": {
                        "namespaces": [self.namespace],
                        "labelSelectors": {tor_pod_group_label_key: healthy_group},
                    },
                },
            },
        }

        networkchaos_manifest_path = self._write_yaml_to_file("tor-networkchaos", networkchaos_manifest)
        self.kubectl.exec_command(f"kubectl apply -f {networkchaos_manifest_path} -n {self.namespace}")

        print(f"Injected network partition: {chaos_resource_name} (faulty <-> healthy) in namespace {self.namespace}")

    def recover_tor_network_partition(self, microservices: list[str]):
        """Recover form network partition created by inject_tor_network_partition() above."""
        chaos_resource_name = "network-segment-policy"
        tor_node_label_key = "network-segment"
        tor_pod_group_label_key = "network-segment"

        # Delete NetworkChaos first to restore network
        self.kubectl.exec_command(
            f"kubectl delete networkchaos {chaos_resource_name} -n {self.namespace} --ignore-not-found=true"
        )
        print(f"Deleted NetworkChaos {chaos_resource_name} (if present).")

        # Remove nodeSelector keys and pod label keys
        dep_names = self.kubectl.exec_command(
            f"kubectl get deployments -n {self.namespace} -o jsonpath='{{.items[*].metadata.name}}'"
        ).split()
        dep_names = [d for d in dep_names if d]
        for dep in dep_names:
            dep_yaml = self._get_deployment_yaml(dep)

            tmpl = dep_yaml.get("spec", {}).get("template", {}) or {}
            tmpl_md = tmpl.get("metadata", {}) or {}
            tmpl_labels = (tmpl_md.get("labels", {}) or {}).copy()
            tmpl_spec = tmpl.get("spec", {}) or {}
            node_selector = (tmpl_spec.get("nodeSelector", {}) or {}).copy()

            changed = False

            if tor_pod_group_label_key in tmpl_labels:
                tmpl_labels.pop(tor_pod_group_label_key, None)
                changed = True

            if tor_node_label_key in node_selector:
                node_selector.pop(tor_node_label_key, None)
                changed = True

            if not changed:
                print(f"[{dep}] No NetworkChaos keys found, skipping redeploying...")
                continue

            if tmpl_labels:
                tmpl_md["labels"] = tmpl_labels
            else:
                tmpl_md.pop("labels", None)

            if node_selector:
                tmpl_spec["nodeSelector"] = node_selector
            else:
                tmpl_spec.pop("nodeSelector", None)

            tmpl["metadata"] = tmpl_md
            tmpl["spec"] = tmpl_spec
            dep_yaml.setdefault("spec", {})["template"] = tmpl

            modified_yaml_path = self._write_yaml_to_file(dep, dep_yaml)

            self.kubectl.exec_command(f"kubectl delete deployment {dep} -n {self.namespace} --ignore-not-found=true")
            self.kubectl.exec_command(f"kubectl apply -f {modified_yaml_path} -n {self.namespace}")

            print(f"[{dep}] Removed {tor_pod_group_label_key} label and {tor_node_label_key} nodeSelector; redeployed.")

        self.kubectl.wait_for_ready(self.namespace)

        # Remove node labels (best effort cleanup)
        nodes = [
            n.metadata.name
            for n in self.kubectl.list_nodes().items
            if "node-role.kubernetes.io/control-plane" not in (n.metadata.labels or {})
            and "node-role.kubernetes.io/master" not in (n.metadata.labels or {})
        ]
        for n in nodes:
            self.kubectl.exec_command(f"kubectl label node {n} {tor_node_label_key}-")

        print(f"Recovered network partition and cleaned node labels ({tor_node_label_key}-).")

    # V.N - init_container_dependency_hang: Pod stuck in Init because an injected
    # init container loops forever waiting on a non-existent dependency (the
    # classic wait-for-it / `until nslookup dep; do sleep; done` pattern, with a
    # typoed / removed service name).  Distinct from `rolling_update_misconfigured`
    # (where an init `sleep infinity` is incidental and the diagnosed root cause
    # is the rolling-update strategy on a synthetic deployment) and from
    # `rbac_misconfiguration` (where an init container fails on PERMISSIONS, not
    # a hang).  See: kubernetes.io init-container docs (recommended dep-wait pattern).
    INIT_DEP_HANG_CONTAINER_NAME = "wait-for-legacy-config"
    INIT_DEP_HANG_TARGET_SVC = "legacy-config-service"

    def inject_init_container_dependency_hang(self, microservices: list[str]):
        """Patch each target deployment to add a busybox init container that loops
        on `nslookup` against a non-existent service, so the pod never leaves
        `Init:0/1`.  Saves the pre-injection deployment manifest for recovery
        and forces a rollout restart so the fault takes effect on the next
        ReplicaSet."""
        for service in microservices:
            get_cmd = f"kubectl get deployment {service} -n {self.namespace} -o yaml"
            original_yaml_str = self.kubectl.exec_command(get_cmd)
            try:
                original = yaml.safe_load(original_yaml_str)
            except yaml.YAMLError as exc:
                raise RuntimeError(
                    f"[init_container_dependency_hang] failed to parse current deployment "
                    f"`{service}` in `{self.namespace}`: {exc}\nRaw kubectl output:\n{original_yaml_str}"
                ) from exc
            if not original or original.get("kind") != "Deployment":
                raise RuntimeError(
                    f"[init_container_dependency_hang] deployment `{service}` not found in "
                    f"`{self.namespace}` (kubectl said: {original_yaml_str[:200]})"
                )

            # Strip runtime/status fields that would make re-apply ugly but keep
            # the spec/metadata/labels intact so recovery is a clean round-trip.
            for noisy in ("status",):
                original.pop(noisy, None)
            meta = original.setdefault("metadata", {})
            for noisy in ("creationTimestamp", "resourceVersion", "uid", "generation", "managedFields"):
                meta.pop(noisy, None)
            # Annotations are kept because Helm releases use them; only drop
            # last-applied so kubectl apply does not warn.
            annotations = meta.get("annotations") or {}
            annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
            if not annotations:
                meta.pop("annotations", None)

            snapshot_path = f"/tmp/{service}_init_dep_hang_original.yaml"
            with open(snapshot_path, "w") as fh:
                yaml.safe_dump(original, fh)
            print(f"Saved pre-injection deployment to {snapshot_path}")

            # Build the dep-wait init container.  We pin `busybox:1.28` because
            # its nslookup exits non-zero on NXDOMAIN; newer busybox releases
            # regressed this (kubernetes/website#12050) and would silently
            # break the fault.  We rely on the exit code alone — an earlier
            # `grep '^Address'` guard matched the resolver's own self-id line
            # in busybox 1.28 output and let the loop terminate immediately
            # even when the target name did not resolve.
            init_cmd = (
                f"echo 'waiting for {self.INIT_DEP_HANG_TARGET_SVC} to become ready...'; "
                f"until nslookup {self.INIT_DEP_HANG_TARGET_SVC}.{self.namespace}.svc.cluster.local "
                f">/dev/null 2>&1; do "
                f"echo 'still waiting on {self.INIT_DEP_HANG_TARGET_SVC}'; sleep 5; done"
            )
            init_container = {
                "name": self.INIT_DEP_HANG_CONTAINER_NAME,
                "image": "busybox:1.28",
                "command": ["/bin/sh", "-c", init_cmd],
            }

            tmpl_spec = original["spec"]["template"]["spec"]
            existing_inits = tmpl_spec.get("initContainers") or []
            # Drop any prior copy of our injected container (defensive: makes
            # inject idempotent if invoked twice on the same cluster).
            existing_inits = [c for c in existing_inits if c.get("name") != self.INIT_DEP_HANG_CONTAINER_NAME]
            existing_inits.append(init_container)
            tmpl_spec["initContainers"] = existing_inits

            faulty_path = f"/tmp/{service}_init_dep_hang_faulty.yaml"
            with open(faulty_path, "w") as fh:
                yaml.safe_dump(original, fh)

            apply_out = self.kubectl.exec_command(f"kubectl apply -f {faulty_path} -n {self.namespace}")
            print(f"Applied init-container hang patch to {service}: {apply_out.strip()}")

            # Force the new ReplicaSet so the fault is visible immediately rather
            # than only on the next legitimate update.
            self.kubectl.exec_command(f"kubectl rollout restart deployment {service} -n {self.namespace}")
            print(f"⚠️  Injected init-container dependency hang into `{service}`")

    def recover_init_container_dependency_hang(self, microservices: list[str]):
        """Reapply the saved pre-injection manifest and force a rollout so the
        cluster returns to its healthy steady state.  Safe to call even if the
        injected init container has already been removed by the agent — the
        round-trip apply is idempotent."""
        for service in microservices:
            snapshot_path = f"/tmp/{service}_init_dep_hang_original.yaml"
            if not Path(snapshot_path).exists():
                # Fall back to stripping our marker init container from whatever
                # is currently deployed.
                get_cmd = f"kubectl get deployment {service} -n {self.namespace} -o yaml"
                current = yaml.safe_load(self.kubectl.exec_command(get_cmd))
                tmpl_spec = current["spec"]["template"]["spec"]
                inits = tmpl_spec.get("initContainers") or []
                tmpl_spec["initContainers"] = [
                    c for c in inits if c.get("name") != self.INIT_DEP_HANG_CONTAINER_NAME
                ] or None
                if tmpl_spec["initContainers"] is None:
                    tmpl_spec.pop("initContainers")
                with open(snapshot_path, "w") as fh:
                    yaml.safe_dump(current, fh)
                print(f"[recover] no snapshot found, reconstructed from live state at {snapshot_path}")

            apply_out = self.kubectl.exec_command(f"kubectl apply -f {snapshot_path} -n {self.namespace}")
            print(f"Restored deployment {service}: {apply_out.strip()}")

            self.kubectl.exec_command(f"kubectl rollout restart deployment {service} -n {self.namespace}")
            self.kubectl.exec_command(f"kubectl rollout status deployment {service} -n {self.namespace} --timeout=120s")
            print(f"✅ Recovered init-container dependency hang for `{service}`")

    def inject_fd_exhaustion(self, microservices: list[str], entrypoint_cmd: str, limit: int = 1024):
        """Injects a file descriptor exhaustion fault by restricting the ulimit."""
        for service in microservices:
            deployment_yaml = self._get_deployment_yaml(service)
            containers = deployment_yaml["spec"]["template"]["spec"]["containers"]
            containers[0]["command"] = ["/bin/sh", "-c"]
            containers[0]["args"] = [f"ulimit -n {limit} && exec {entrypoint_cmd}"]

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)
            apply_result = self.kubectl.exec_command(f"kubectl apply -f {modified_yaml_path} -n {self.namespace}")
            print(f"Apply result for {service}: {apply_result}")

            self.kubectl.exec_command(f"kubectl rollout status deployment {service} -n {self.namespace} --timeout=120s")
            print(f"Injected FD exhaustion (limit: {limit}) for service: {service}")

    def recover_fd_exhaustion(self, microservices: list[str], entrypoint_cmd: str):
        """Recover from FD exhaustion by pushing the soft limit to the kernel hard limit."""
        for service in microservices:
            deployment_yaml = self._get_deployment_yaml(service)

            containers = deployment_yaml["spec"]["template"]["spec"]["containers"]
            containers[0]["command"] = [entrypoint_cmd]
            containers[0]["args"] = []

            modified_yaml_path = self._write_yaml_to_file(service, deployment_yaml)

            apply_result = self.kubectl.exec_command(f"kubectl apply -f {modified_yaml_path} -n {self.namespace}")
            print(f"Recover apply result for {service}: {apply_result}")

            self.kubectl.exec_command(f"kubectl rollout status deployment {service} -n {self.namespace} --timeout=120s")
            print(f"Recovered FD exhaustion for service: {service}")

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

    def _get_daemon_set_yaml(self, daemon_set_name: str) -> dict | None:
        daemon_set_yaml = self.kubectl.exec_command(f"kubectl get ds {daemon_set_name} -n {self.namespace} -o yaml")
        parsed = yaml.safe_load(daemon_set_yaml)
        if not isinstance(parsed, dict):
            print(f"[inject_virtual] Failed to get daemonset '{daemon_set_name}': {daemon_set_yaml[:200]}")
            return None
        return parsed

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

    def scale_pods_to(self, replicas: int, microservices: list[str]):
        """Inject a fault to scale pods to zero for a service."""
        for service in microservices:
            self.kubectl.exec_command(f"kubectl scale deployment {service} --replicas={replicas} -n {self.namespace}")
            print(f"Scaled deployment {service} to {replicas} replicas | namespace: {self.namespace}")

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

            target_pods = [pod for pod in pods.items if (label_selector or service in pod.metadata.name)]

            if not target_pods:
                time.sleep(sleep)
                waited += sleep
                continue

            state_ok = True

            for pod in target_pods:
                dns_config = pod.spec.dns_config
                nameservers = dns_config.nameservers if dns_config and dns_config.nameservers else []
                has_external = external_ns in nameservers

                if expect_external != has_external:
                    state_ok = False
                    break

            if state_ok:
                return

            time.sleep(sleep)
            waited += sleep

        print(f"DNS policy propagation check for service '{service}' failed after {max_wait}s.")


if __name__ == "__main__":
    namespace = "social-network"
    microservices = ["mongodb-geo"]
    # microservices = ["geo"]
    fault_type = "auth_miss_mongodb"
    # fault_type = "misconfig_app"
    # fault_type = "revoke_auth"
    print("Start injection ...")
    injector = VirtualizationFaultInjector(namespace)
    # injector._inject(fault_type, microservices)
    injector._recover(fault_type, microservices)
