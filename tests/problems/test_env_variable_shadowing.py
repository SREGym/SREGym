from types import SimpleNamespace

from kubernetes import client

from sregym.conductor.problems.env_variable_shadowing import EnvVariableShadowing


def _env(name, value):
    return client.V1EnvVar(name=name, value=value)


def _template(*host_values, annotations=None):
    environment = [_env("ENVOY_PORT", "8080")]
    environment.extend(_env("FRONTEND_HOST", value) for value in host_values)
    return client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(annotations=annotations or {}),
        spec=client.V1PodSpec(
            containers=[
                client.V1Container(name="sidecar", image="sidecar:latest"),
                client.V1Container(
                    name="frontend-proxy",
                    image="frontend-proxy:latest",
                    env=environment,
                ),
            ]
        ),
    )


def _deployment(template, *, replicas=1, strategy="agent-strategy"):
    return SimpleNamespace(
        spec=SimpleNamespace(
            template=template,
            replicas=replicas,
            strategy=strategy,
        )
    )


class _AppsV1:
    def __init__(self):
        self.patches = []
        self.replacements = []

    def patch_namespaced_deployment(self, name, namespace, body):
        self.patches.append((name, namespace, body))

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
    problem = EnvVariableShadowing.__new__(EnvVariableShadowing)
    problem.kubectl = kubectl
    problem.namespace = "astronomy-shop"
    problem.faulty_service = "frontend-proxy"
    problem._baseline_template = None
    return problem


def _host_values(template):
    proxy = next(container for container in template.spec.containers if container.name == "frontend-proxy")
    return [item.value for item in proxy.env if item.name == "FRONTEND_HOST"]


def test_injection_appends_a_later_shadowing_definition_with_json_patch():
    kubectl = _KubeCtl(_deployment(_template("frontend")))
    problem = _problem(kubectl)

    problem._append_shadowing_definition()

    assert kubectl.apps_v1_api.patches == [
        (
            "frontend-proxy",
            "astronomy-shop",
            [
                {
                    "op": "add",
                    "path": "/spec/template/spec/containers/1/env/-",
                    "value": {"name": "FRONTEND_HOST", "value": "localhost"},
                }
            ],
        )
    ]


def test_injection_rejects_an_already_ambiguous_baseline():
    problem = _problem(_KubeCtl(_deployment(_template("frontend", "localhost"))))

    try:
        problem._append_shadowing_definition()
    except RuntimeError as exc:
        assert "exactly one FRONTEND_HOST=frontend" in str(exc)
    else:
        raise AssertionError("Expected an ambiguous baseline to be rejected")


def test_capture_preserves_the_original_single_definition():
    original = _template("frontend")
    problem = _problem(_KubeCtl(_deployment(original)))

    problem._capture_baseline_template()
    original.spec.containers[1].env.append(_env("FRONTEND_HOST", "localhost"))

    assert _host_values(problem._baseline_template) == ["frontend"]


def test_restore_replaces_template_but_preserves_latest_deployment_settings():
    baseline = _template("frontend", annotations={"baseline": "true"})
    current = _deployment(
        _template("frontend", "localhost", annotations={"agent": "extra-rollout"}),
        replicas=3,
        strategy="agent-strategy",
    )
    kubectl = _KubeCtl(current)
    problem = _problem(kubectl)
    problem._baseline_template = baseline

    problem._restore_baseline_template()

    assert len(kubectl.apps_v1_api.replacements) == 1
    _, _, replacement = kubectl.apps_v1_api.replacements[0]
    assert _host_values(replacement.spec.template) == ["frontend"]
    assert replacement.spec.template.metadata.annotations == {"baseline": "true"}
    assert replacement.spec.replicas == 3
    assert replacement.spec.strategy == "agent-strategy"


def test_recovery_waits_for_rollout_and_clears_snapshot():
    kubectl = _KubeCtl(_deployment(_template("frontend", "localhost")))
    problem = _problem(kubectl)
    problem._baseline_template = _template("frontend")

    problem.recover_fault()

    assert len(kubectl.apps_v1_api.replacements) == 1
    assert any("rollout status deployment/frontend-proxy" in command for command in kubectl.commands)
    assert problem._baseline_template is None
    assert problem.fault_injected is False
