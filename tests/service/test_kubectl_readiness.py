from types import SimpleNamespace
from unittest.mock import Mock

from sregym.service.kubectl import KubeCtl


def _ready_pod(*, terminating: bool):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            deletion_timestamp=object() if terminating else None,
            owner_references=[],
        ),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(ready=True)],
        ),
    )


def test_wait_for_ready_blocks_until_terminating_pod_is_gone(monkeypatch):
    current = _ready_pod(terminating=False)
    draining = _ready_pod(terminating=True)
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.get_service = Mock(
        return_value=SimpleNamespace(spec=SimpleNamespace(selector={"app": "recommendation"}))
    )
    kubectl.core_v1_api = SimpleNamespace(
        list_namespaced_pod=Mock(
            side_effect=[
                SimpleNamespace(items=[current, draining]),
                SimpleNamespace(items=[current]),
            ]
        )
    )
    monkeypatch.setattr("sregym.service.kubectl.time.sleep", lambda _: None)

    kubectl.wait_for_ready(
        "astronomy-shop",
        service_names="recommendation",
        sleep=1,
        max_wait=2,
    )

    assert kubectl.core_v1_api.list_namespaced_pod.call_count == 2
