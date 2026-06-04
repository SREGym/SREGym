import contextlib
import re
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.mitigation import MitigationOracle


class EndpointSliceStaleAfterRolloutMitigationOracle(MitigationOracle):
    importance = 1.0

    def __init__(self, problem):
        super().__init__(problem)
        self.core_v1 = client.CoreV1Api()

    def evaluate(self) -> dict:
        print("== EndpointSlice Stale After Rollout Mitigation Evaluation ==")
        results = super().evaluate()
        if not results.get("success"):
            return results

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        service_name = self.problem.faulty_service

        service_config = kubectl.get_service_json(service_name, namespace)
        selector = service_config.get("spec", {}).get("selector") or {}

        pod_ips = self._get_current_pod_ips(namespace, selector)
        endpoint_ips = self._get_endpointslice_ips(namespace, service_name)

        current_ips = set(pod_ips)
        service_ips = set(endpoint_ips)
        stale_ips = sorted(service_ips - current_ips)
        missing_ips = sorted(current_ips - service_ips)

        if stale_ips or missing_ips:
            if stale_ips:
                print(f"❌ Found stale EndpointSlice addresses not matching running pods: {stale_ips}")
                results["stale_endpoint_ips"] = stale_ips
            if missing_ips:
                print(f"❌ EndpointSlice is missing addresses for running pods: {missing_ips}")
                results["missing_endpoint_ips"] = missing_ips
            results["success"] = False
            return results

        probe_ok = self._probe_service(namespace, service_name)
        results["frontend_probe"] = probe_ok
        results["success"] = results["success"] and probe_ok

        if probe_ok:
            print("✅ Frontend service probe succeeded")
        else:
            print("❌ Frontend service probe failed")

        return results

    def _get_current_pod_ips(self, namespace: str, selector: dict) -> list[str]:
        pods = self.problem.kubectl.list_pods(namespace)
        return [
            pod.status.pod_ip
            for pod in pods.items
            if pod.status and pod.status.pod_ip and self._match_selector(pod, selector)
        ]

    def _match_selector(self, pod, selector: dict) -> bool:
        if not selector:
            return True
        labels = pod.metadata.labels or {}
        return all(labels.get(k) == v for k, v in selector.items())

    def _get_endpointslice_ips(self, namespace: str, service_name: str) -> list[str]:
        discovery = client.DiscoveryV1Api()
        slice_list = discovery.list_namespaced_endpoint_slice(
            namespace=namespace,
            label_selector=f"kubernetes.io/service-name={service_name}",
        )
        ips = []
        for endpoint_slice in slice_list.items:
            for endpoint in endpoint_slice.endpoints or []:
                ips.extend(endpoint.addresses or [])
        return ips

    def _probe_service(self, namespace: str, service_name: str) -> bool:
        pod_name = f"endpointslice-probe-{int(time.time() * 1000)}"
        script = (
            "ok=0; fail=0; "
            "for i in $(seq 1 5); do "
            f"wget -q -T 2 -O /dev/null http://{service_name}:5000/ && ok=$((ok+1)) || fail=$((fail+1)); "
            "sleep 0.1; done; "
            "echo PROBE_OK=$ok PROBE_FAIL=$fail; "
            'test "$fail" -le 1'
        )
        pod = {
            "metadata": {"name": pod_name, "namespace": namespace, "labels": {"app": "endpointslice-probe"}},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "containers": [{"name": "probe", "image": "busybox:1.36", "command": ["sh", "-c", script]}],
            },
        }

        try:
            self.core_v1.create_namespaced_pod(namespace=namespace, body=pod)
            phase = self._wait_for_probe_pod(pod_name, namespace)
            logs = self.core_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace)
            print(logs.strip())
            match = re.search(r"PROBE_FAIL=(\d+)", logs)
            if not match:
                return False
            return int(match.group(1)) <= 1 and phase == "Succeeded"
        except ApiException as e:
            print(f"Probe pod failed: {e}")
            return False
        finally:
            with contextlib.suppress(ApiException):
                self.core_v1.delete_namespaced_pod(
                    name=pod_name, namespace=namespace, body=client.V1DeleteOptions(propagation_policy="Foreground")
                )

    def _wait_for_probe_pod(self, pod_name: str, namespace: str, timeout: int = 60) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pod = self.core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
            phase = pod.status.phase
            if phase in ("Succeeded", "Failed"):
                return phase
            time.sleep(1)
        return "Failed"
