from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.problems.incorrect_image import IncorrectImage


@pytest.fixture
def problem():
    instance = IncorrectImage.__new__(IncorrectImage)
    instance.namespace = "astronomy-shop"
    instance.faulty_service = ["product-catalog"]
    instance._original_images = {}
    instance.injector = Mock()
    instance.kubectl = Mock()
    instance.deployment = SimpleNamespace(
        metadata=SimpleNamespace(uid="original-deployment"),
        spec=SimpleNamespace(
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[
                        SimpleNamespace(name="product-catalog", image="registry.test/catalog:v9@sha256:abc"),
                        SimpleNamespace(name="sidecar", image="registry.test/sidecar:v1"),
                    ]
                )
            )
        ),
    )
    instance.kubectl.get_deployment.return_value = instance.deployment

    def inject(**kwargs):
        instance.deployment.spec.template.spec.containers[0].image = kwargs["bad_image"]

    instance.injector.inject_incorrect_image.side_effect = inject
    return instance


def test_recovery_uses_original_digest_and_container_name_after_reordering(problem):
    problem.inject_fault()
    problem.inject_fault()
    problem.deployment.spec.template.spec.containers.reverse()
    problem.recover_fault()

    problem.kubectl.patch_deployment.assert_called_once_with(
        name="product-catalog",
        namespace="astronomy-shop",
        patch_body={
            "metadata": {"uid": "original-deployment"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "product-catalog", "image": "registry.test/catalog:v9@sha256:abc"}]
                    }
                }
            },
        },
    )


def test_partial_injection_failure_keeps_original_image(problem):
    problem.injector.inject_incorrect_image.side_effect = RuntimeError("patch failed")
    with pytest.raises(RuntimeError, match="patch failed"):
        problem.inject_fault()
    problem.recover_fault()
    problem.kubectl.patch_deployment.assert_called_once()


def test_recovery_without_injection_does_not_guess_an_image(problem, capsys):
    # The shared recovery decorator reports errors instead of re-raising them.
    problem.recover_fault()
    problem.kubectl.patch_deployment.assert_not_called()
    assert "refusing to guess" in capsys.readouterr().out


def test_recovery_rejects_a_recreated_deployment(problem, capsys):
    problem.inject_fault()
    problem.deployment.metadata.uid = "another-deployment"
    problem.recover_fault()
    problem.kubectl.patch_deployment.assert_not_called()
    assert "refusing to restore a stale image" in capsys.readouterr().out


def test_reinjection_rejects_a_recreated_deployment(problem):
    problem.inject_fault()
    problem.deployment.metadata.uid = "another-deployment"
    with pytest.raises(RuntimeError, match="recreated"):
        problem.inject_fault()
    assert problem.injector.inject_incorrect_image.call_count == 1


def test_recovery_rejects_a_removed_container(problem, capsys):
    problem.inject_fault()
    problem.deployment.spec.template.spec.containers.pop(0)
    problem.recover_fault()
    problem.kubectl.patch_deployment.assert_not_called()
    assert "Original container 'product-catalog' is missing" in capsys.readouterr().out


def test_recovery_can_retry_a_failed_patch(problem):
    problem.inject_fault()
    problem.kubectl.patch_deployment.side_effect = [RuntimeError("patch failed"), None]
    problem.recover_fault()
    problem.recover_fault()
    assert problem.kubectl.patch_deployment.call_count == 2
