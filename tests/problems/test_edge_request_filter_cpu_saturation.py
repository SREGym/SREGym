from types import SimpleNamespace

from kubernetes import client

from sregym.conductor.problems.edge_request_filter_cpu_saturation import (
    EdgeRequestFilterCPUSaturation,
)


def _template(command, *, annotations=None):
    return client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(
            labels={"app.kubernetes.io/name": "frontend-proxy"},
            annotations=annotations or {},
        ),
        spec=client.V1PodSpec(
            containers=[
                client.V1Container(
                    name="frontend-proxy",
                    image="frontend-proxy:latest",
                    command=command,
                )
            ]
        ),
    )


def _deployment(template, *, replicas=2, strategy="agent-strategy"):
    return SimpleNamespace(
        spec=SimpleNamespace(
            template=template,
            replicas=replicas,
            strategy=strategy,
        )
    )


class _AppsV1:
    def __init__(self):
        self.replacements = []

    def replace_namespaced_deployment(self, name, namespace, body):
        self.replacements.append((name, namespace, body))


class _KubeCtl:
    def __init__(self, deployment):
        self.deployment = deployment
        self.apps_v1_api = _AppsV1()
        self.commands = []

    def get_deployment(self, name, namespace):
        return self.deployment

    def exec_command(self, command):
        self.commands.append(command)
        return ""


def _problem(kubectl):
    problem = EdgeRequestFilterCPUSaturation.__new__(EdgeRequestFilterCPUSaturation)
    problem.kubectl = kubectl
    problem.namespace = "astronomy-shop"
    problem.faulty_service = "frontend-proxy"
    problem.traffic_source = "load-generator"
    problem.process_marker = "edge-traffic-replay"
    problem.driver_log = "/tmp/edge-traffic-replay.log"
    problem.driver_pid = "/tmp/edge-traffic-replay.pid"
    problem._baseline_template = None
    return problem


def test_capture_keeps_an_independent_copy_of_the_baseline_template():
    original = _template(None, annotations={"baseline": "true"})
    problem = _problem(_KubeCtl(_deployment(original)))

    problem._capture_baseline_template()
    original.metadata.annotations["baseline"] = "mutated"
    original.spec.containers[0].command = ["faulted"]

    assert problem._baseline_template.metadata.annotations["baseline"] == "true"
    assert problem._baseline_template.spec.containers[0].command is None


def test_capture_does_not_overwrite_the_original_baseline_on_repeated_injection():
    original = _template(None)
    kubectl = _KubeCtl(_deployment(original))
    problem = _problem(kubectl)
    problem._capture_baseline_template()

    kubectl.deployment.spec.template = _template(["faulted"])
    problem._capture_baseline_template()

    assert problem._baseline_template.spec.containers[0].command is None


def test_restore_replaces_only_template_on_latest_deployment():
    baseline = _template(None, annotations={"baseline": "true"})
    current = _deployment(
        _template(["agent-rollout"], annotations={"agent": "kept-outside-template"}),
        replicas=3,
        strategy="agent-strategy",
    )
    kubectl = _KubeCtl(current)
    problem = _problem(kubectl)
    problem._baseline_template = baseline

    problem._restore_baseline_template()

    assert len(kubectl.apps_v1_api.replacements) == 1
    name, namespace, replacement = kubectl.apps_v1_api.replacements[0]
    assert (name, namespace) == ("frontend-proxy", "astronomy-shop")
    assert replacement.spec.template.metadata.annotations == {"baseline": "true"}
    assert replacement.spec.template.spec.containers[0].command is None
    assert replacement.spec.replicas == 3
    assert replacement.spec.strategy == "agent-strategy"


def test_recovery_restores_snapshot_instead_of_undoing_previous_revision():
    baseline = _template(None)
    kubectl = _KubeCtl(_deployment(_template(["agent-rollout"])))
    problem = _problem(kubectl)
    problem._baseline_template = baseline

    problem.recover_fault()

    assert len(kubectl.apps_v1_api.replacements) == 1
    assert not any("rollout undo" in command for command in kubectl.commands)
    assert any("rollout status deployment/frontend-proxy" in command for command in kubectl.commands)
    cleanup_command = next(command for command in kubectl.commands if "kubectl exec" in command)
    assert "/proc" in cleanup_command
    assert "signal.SIGTERM" in cleanup_command
    assert problem._baseline_template is None
    assert problem.fault_injected is False


def test_restore_requires_a_captured_baseline():
    problem = _problem(_KubeCtl(_deployment(_template(["faulted"]))))

    try:
        problem._restore_baseline_template()
    except RuntimeError as exc:
        assert "captured baseline" in str(exc)
    else:
        raise AssertionError("Expected recovery without a baseline to fail")
