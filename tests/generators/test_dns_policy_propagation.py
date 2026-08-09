from types import SimpleNamespace

from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector


def _dns_injector(nameservers):
    deployment = SimpleNamespace(spec=SimpleNamespace(selector=SimpleNamespace(match_labels={"app": "frontend"})))
    pod = SimpleNamespace(
        metadata=SimpleNamespace(name="frontend-abc123"),
        spec=SimpleNamespace(dns_config=SimpleNamespace(nameservers=nameservers)),
    )
    kubectl = SimpleNamespace(
        apps_v1_api=SimpleNamespace(read_namespaced_deployment=lambda name, namespace: deployment),
        core_v1_api=SimpleNamespace(list_namespaced_pod=lambda namespace, label_selector: SimpleNamespace(items=[pod])),
    )
    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "astronomy-shop"
    injector.kubectl = kubectl
    return injector


def test_dns_propagation_accepts_injected_external_nameserver_without_exec():
    injector = _dns_injector(["8.8.8.8"])

    injector._wait_for_dns_policy_propagation(
        "frontend",
        external_ns="8.8.8.8",
        expect_external=True,
        sleep=0,
        max_wait=1,
    )


def test_dns_propagation_accepts_recovery_without_external_nameserver():
    injector = _dns_injector(None)

    injector._wait_for_dns_policy_propagation(
        "frontend",
        external_ns="8.8.8.8",
        expect_external=False,
        sleep=0,
        max_wait=1,
    )
