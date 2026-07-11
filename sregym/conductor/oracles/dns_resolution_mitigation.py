import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class DNSResolutionMitigationOracle(Oracle):
    importance = 1.0
    probe_timeout_seconds = 60
    rollout_timeout_seconds = 120
    poll_interval_seconds = 2

    @staticmethod
    def _rollout_complete(deployment) -> bool:
        desired = deployment.spec.replicas
        if desired is None:
            desired = 1
        if desired < 1:
            return False

        generation = deployment.metadata.generation or 0
        status = deployment.status
        return (
            (status.observed_generation or 0) >= generation
            and (status.updated_replicas or 0) == desired
            and (status.ready_replicas or 0) == desired
            and (status.available_replicas or 0) == desired
            and (status.unavailable_replicas or 0) == 0
        )

    def _wait_for_current_rollout(self, deployment):
        deadline = time.monotonic() + self.rollout_timeout_seconds
        while True:
            if self._rollout_complete(deployment):
                return deployment
            if time.monotonic() >= deadline:
                return None

            time.sleep(self.poll_interval_seconds)
            deployment = self.problem.kubectl.get_deployment(deployment.metadata.name, self.problem.namespace)

    def _find_service_and_deployment(self, services):
        faulty_service = self.problem.faulty_service
        if faulty_service is not None:
            service = next((svc for svc in services if svc.metadata.name == faulty_service), None)
            if service is None:
                return None, None
            return service, self.problem.kubectl.get_deployment(faulty_service, self.problem.namespace)

        for service in services:
            if not service.spec.selector:
                continue
            try:
                deployment = self.problem.kubectl.get_deployment(service.metadata.name, self.problem.namespace)
                return service, deployment
            except ApiException as exc:
                if exc.status != 404:
                    raise
        return None, None

    def _run_dns_probe(self, deployment, dns_name: str, service_port: int) -> bool:
        namespace = self.problem.namespace
        core_v1 = self.problem.kubectl.core_v1_api
        source_spec = deployment.spec.template.spec
        pod_name = f"dns-readiness-check-{time.time_ns()}"[:63]
        script = f"nslookup {dns_name} >/dev/null && nc -z -w 5 {dns_name} {service_port} && echo DNS_OK"
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels={"app": "dns-readiness-check"},
            ),
            spec=client.V1PodSpec(
                restart_policy="Never",
                automount_service_account_token=False,
                dns_policy=source_spec.dns_policy,
                dns_config=source_spec.dns_config,
                containers=[
                    client.V1Container(
                        name="probe",
                        image="busybox:1.36",
                        image_pull_policy="IfNotPresent",
                        command=["sh", "-c", script],
                    )
                ],
            ),
        )

        try:
            core_v1.create_namespaced_pod(namespace=namespace, body=pod)
            deadline = time.monotonic() + self.probe_timeout_seconds
            phase = "Pending"
            while time.monotonic() < deadline:
                current = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
                phase = current.status.phase or "Pending"
                if phase in ("Succeeded", "Failed"):
                    break
                time.sleep(self.poll_interval_seconds)

            logs = core_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace)
            print(logs.strip())
            return phase == "Succeeded" and "DNS_OK" in logs
        except ApiException as exc:
            print(f"❌ DNS probe pod failed: {exc}")
            return False
        finally:
            with contextlib.suppress(ApiException):
                core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace, grace_period_seconds=0)

    def evaluate(self) -> dict:
        print("== DNS Resolution Mitigation Check ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        try:
            service, deployment = self._find_service_and_deployment(kubectl.list_services(namespace).items)
            if service is None or deployment is None:
                print(f"❌ Service or Deployment {self.problem.faulty_service or '<with selector>'} not found.")
                return {"success": False}

            deployment = self._wait_for_current_rollout(deployment)
            if deployment is None:
                print("❌ Affected Deployment did not complete its current rollout.")
                return {"success": False}

            service_ports = service.spec.ports or []
            if not service_ports or service_ports[0].port is None:
                print(f"❌ Service {service.metadata.name} has no usable port.")
                return {"success": False}

            dns_name = f"{service.metadata.name}.{namespace}.svc.cluster.local"
            service_port = service_ports[0].port
            if not self._run_dns_probe(deployment, dns_name, service_port):
                print(f"[❌] Failed DNS resolution or TCP connection for {dns_name}:{service_port}")
                return {"success": False}

            print(
                f"[✅] Successfully resolved and connected to {dns_name}:{service_port} "
                f"using deployment/{deployment.metadata.name} DNS settings"
            )
            return {"success": True}
        except Exception as exc:
            print(f"❌ Error checking DNS resolution: {exc}")
            return {"success": False}
