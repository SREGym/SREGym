"""Deadline handling in wait_for_namespace_deletion and wait_for_stable.

Both loops bound max_wait with a monotonic deadline, as wait_for_ready does.
These tests check that each loop exits when sleep is 0 and returns as soon as
the condition holds.
"""

import time
from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.service.kubectl import KubeCtl


def _fake_clock(monkeypatch, *, tick):
    """Replace time.monotonic and time.sleep with a controlled clock.

    sleep advances the clock by at least ``tick`` so a sleep=0 loop still
    makes progress toward the deadline.
    """
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + max(seconds, tick)))
    return now


def _pod(*, ready):
    status = client.V1ContainerStatus(
        name="app",
        image="example/app:latest",
        image_id="",
        ready=ready,
        restart_count=0,
        state=None if ready else client.V1ContainerState(waiting=client.V1ContainerStateWaiting(reason="Pending")),
    )
    return client.V1Pod(
        metadata=client.V1ObjectMeta(name="app-123"),
        status=client.V1PodStatus(phase="Running", container_statuses=[status], conditions=[]),
    )


def test_namespace_deletion_sleep_zero_still_terminates(monkeypatch):
    _fake_clock(monkeypatch, tick=0.05)
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl.core_v1_api.read_namespace.return_value = client.V1Namespace(metadata=client.V1ObjectMeta(name="ns"))

    with pytest.raises(Exception, match="Timeout"):
        kubectl.wait_for_namespace_deletion("ns", sleep=0, max_wait=1)

    assert kubectl.core_v1_api.read_namespace.call_count > 1


def test_namespace_deletion_returns_once_the_namespace_is_gone(monkeypatch):
    _fake_clock(monkeypatch, tick=0.0)
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.core_v1_api = Mock()
    kubectl.core_v1_api.read_namespace.side_effect = ApiException(status=404, reason="Not Found")

    kubectl.wait_for_namespace_deletion("ns", sleep=2, max_wait=600)

    assert kubectl.core_v1_api.read_namespace.call_count == 1


def test_stable_sleep_zero_still_terminates(monkeypatch):
    _fake_clock(monkeypatch, tick=0.05)
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.list_pods = Mock(return_value=client.V1PodList(items=[_pod(ready=False)]))

    with pytest.raises(Exception, match="Timeout"):
        kubectl.wait_for_stable("ns", sleep=0, max_wait=1)

    assert kubectl.list_pods.call_count > 1


def test_stable_returns_once_every_pod_is_ready(monkeypatch):
    _fake_clock(monkeypatch, tick=0.0)
    kubectl = KubeCtl.__new__(KubeCtl)
    kubectl.list_pods = Mock(return_value=client.V1PodList(items=[_pod(ready=True)]))

    kubectl.wait_for_stable("ns", sleep=2, max_wait=600)

    assert kubectl.list_pods.call_count == 1
