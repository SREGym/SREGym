from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client import ApiException

from sregym.generators.workload import blueprint_hotel_work as workload_module
from sregym.generators.workload.blueprint_hotel_work import (
    CPU_STRESS_DAEMONSET_NAME,
    CPU_STRESS_NAMESPACE,
    BHotelWrkWorkloadManager,
)


@pytest.fixture
def workload():
    workload = BHotelWrkWorkloadManager.__new__(BHotelWrkWorkloadManager)
    workload.namespace = "blueprint-hotel-reservation"
    workload.CPU_containment = True
    workload.continuous = True
    workload.deployment_name = "bhotelwrk-wlgen"
    workload.job_name = "bhotelwrk-wlgen-job"
    workload.apply_capacity_restraint = True
    workload.wrk = Mock()
    workload.cpu_containment_injector = Mock()
    workload.current_experiment_names = ["network-latency-all-pods"]
    return workload


def test_cleanup_deletes_the_fixed_target_without_a_successful_create(workload, monkeypatch):
    api = Mock()
    monkeypatch.setattr(workload_module.client, "AppsV1Api", lambda: api)

    workload._delete_cpu_stress_daemonset()

    call = api.delete_namespaced_daemon_set.call_args.kwargs
    assert call["name"] == CPU_STRESS_DAEMONSET_NAME
    assert call["namespace"] == CPU_STRESS_NAMESPACE
    assert call["body"].propagation_policy == "Foreground"
    assert call["_request_timeout"] == 30
    api.read_namespaced_daemon_set.assert_not_called()


def test_final_cleanup_waits_for_both_controller_and_terminating_pods(workload, monkeypatch):
    api = Mock()
    core = Mock()
    api.read_namespaced_daemon_set.side_effect = [Mock(), ApiException(status=404), ApiException(status=404)]
    core.list_namespaced_pod.side_effect = [SimpleNamespace(items=[Mock()]), SimpleNamespace(items=[])]
    monkeypatch.setattr(workload_module.client, "AppsV1Api", lambda: api)
    monkeypatch.setattr(workload_module.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(workload_module.time, "sleep", Mock())

    workload._delete_cpu_stress_daemonset(wait=True)

    assert api.read_namespaced_daemon_set.call_count == 3
    assert core.list_namespaced_pod.call_count == 2
    for call in core.list_namespaced_pod.call_args_list:
        assert call.kwargs["namespace"] == CPU_STRESS_NAMESPACE
        assert call.kwargs["label_selector"] == f"app={CPU_STRESS_DAEMONSET_NAME}"


@pytest.mark.parametrize("controller_exists", [False, True])
def test_final_cleanup_reports_stuck_controllers_or_pods_within_a_bounded_wait(
    workload, monkeypatch, controller_exists
):
    api = Mock()
    core = Mock()
    if not controller_exists:
        api.read_namespaced_daemon_set.side_effect = ApiException(status=404)
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[Mock()])
    clock = [0.0]
    monkeypatch.setattr(workload_module.client, "AppsV1Api", lambda: api)
    monkeypatch.setattr(workload_module.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(workload_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(workload_module.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    with pytest.raises(TimeoutError, match="remain after 60 seconds"):
        workload._delete_cpu_stress_daemonset(wait=True)

    assert clock[0] == 60
    assert all(0 < call.kwargs["_request_timeout"] <= 30 for call in api.read_namespaced_daemon_set.call_args_list)
    assert all(type(call.kwargs["_request_timeout"]) is int for call in api.read_namespaced_daemon_set.call_args_list)


def test_final_cleanup_accepts_an_already_deleted_namespace(workload, monkeypatch):
    api = Mock()
    core = Mock()
    api.delete_namespaced_daemon_set.side_effect = ApiException(status=404)
    api.read_namespaced_daemon_set.side_effect = ApiException(status=404)
    core.list_namespaced_pod.side_effect = ApiException(status=404)
    monkeypatch.setattr(workload_module.client, "AppsV1Api", lambda: api)
    monkeypatch.setattr(workload_module.client, "CoreV1Api", lambda: core)

    workload._delete_cpu_stress_daemonset(wait=True)


def test_final_cleanup_does_not_treat_a_failed_read_as_absence(workload, monkeypatch):
    api = Mock()
    api.read_namespaced_daemon_set.side_effect = ApiException(status=503)
    monkeypatch.setattr(workload_module.client, "AppsV1Api", lambda: api)
    monkeypatch.setattr(workload_module.client, "CoreV1Api", Mock())

    with pytest.raises(ApiException) as caught:
        workload._delete_cpu_stress_daemonset(wait=True)

    assert caught.value.status == 503


@pytest.mark.parametrize("error", [ApiException(status=403), ApiException(status=500), TimeoutError("API timeout")])
def test_stress_deletion_errors_are_not_reported_as_success(workload, monkeypatch, error):
    api = Mock()
    api.delete_namespaced_daemon_set.side_effect = error
    monkeypatch.setattr(workload_module.client, "AppsV1Api", lambda: api)

    with pytest.raises(type(error)) as caught:
        workload._delete_cpu_stress_daemonset()

    assert caught.value is error


def test_missing_stress_resource_is_safe_to_clean_repeatedly(workload, monkeypatch):
    api = Mock()
    api.delete_namespaced_daemon_set.side_effect = ApiException(status=404)
    monkeypatch.setattr(workload_module.client, "AppsV1Api", lambda: api)
    del workload.current_experiment_names

    workload.cleanup_cpu_stress()
    workload.cleanup_cpu_stress()

    assert api.delete_namespaced_daemon_set.call_count == 2
    workload.cpu_containment_injector.delete_chaos_experiment.assert_not_called()


@pytest.mark.parametrize("wait", [False, True])
def test_network_cleanup_failure_does_not_skip_stress_cleanup_and_can_be_retried(workload, wait):
    workload._delete_cpu_stress_daemonset = Mock()
    delete_network = workload.cpu_containment_injector.delete_chaos_experiment
    delete_network.side_effect = RuntimeError("NetworkChaos deletion failed")

    with pytest.raises(RuntimeError, match="NetworkChaos deletion failed"):
        workload.cleanup_cpu_stress(wait=wait)

    workload._delete_cpu_stress_daemonset.assert_called_once_with(wait=wait)
    assert workload.current_experiment_names == ["network-latency-all-pods"]

    delete_network.side_effect = None
    workload.cleanup_cpu_stress()
    workload.cleanup_cpu_stress()

    assert delete_network.call_count == 2
    assert workload._delete_cpu_stress_daemonset.call_count == 3
    assert workload.current_experiment_names == []


def test_stress_cleanup_failure_does_not_repeat_successful_network_deletion(workload):
    workload._delete_cpu_stress_daemonset = Mock(side_effect=[RuntimeError("stress deletion failed"), None])

    with pytest.raises(RuntimeError, match="stress deletion failed"):
        workload.cleanup_cpu_stress()
    workload.cleanup_cpu_stress()

    workload.cpu_containment_injector.delete_chaos_experiment.assert_called_once_with("network-latency-all-pods")


@pytest.mark.parametrize("apply_restraint", [False, True])
def test_normal_trigger_recovery_preserves_capacity_restraint_setting(workload, apply_restraint):
    workload.apply_capacity_restraint = apply_restraint
    calls = Mock()
    calls.attach_mock(Mock(), "cleanup")
    calls.attach_mock(Mock(), "restraint")
    workload.cleanup_cpu_stress = calls.cleanup
    workload._apply_capacity_restraint = calls.restraint

    workload._recover_cpu_stress()

    assert [call[0] for call in calls.mock_calls] == (["cleanup", "restraint"] if apply_restraint else ["cleanup"])


def test_failed_trigger_cleanup_aborts_before_applying_capacity_restraint(workload):
    workload.cleanup_cpu_stress = Mock(side_effect=RuntimeError("trigger cleanup failed"))
    workload._apply_capacity_restraint = Mock()

    with pytest.raises(RuntimeError, match="trigger cleanup failed"):
        workload._recover_cpu_stress()

    workload._apply_capacity_restraint.assert_not_called()


@pytest.mark.parametrize("failed_step", ["network", "stress"])
def test_partial_injection_keeps_the_network_cleanup_target(workload, failed_step):
    del workload.current_experiment_names
    workload._deploy_cpu_stress_daemonset = Mock()
    workload._delete_cpu_stress_daemonset = Mock()
    if failed_step == "network":
        workload.cpu_containment_injector.create_chaos_experiment.side_effect = RuntimeError("create failed")
    else:
        workload._deploy_cpu_stress_daemonset.side_effect = RuntimeError("create failed")

    workload._inject_cpu_stress()
    assert workload.current_experiment_names == ["network-latency-all-pods"]
    workload.cleanup_cpu_stress()

    workload.cpu_containment_injector.delete_chaos_experiment.assert_called_once_with("network-latency-all-pods")
    workload._delete_cpu_stress_daemonset.assert_called_once_with(wait=False)


@pytest.mark.parametrize("continuous", [False, True])
@pytest.mark.parametrize("stop_fails", [False, True])
def test_workload_stop_always_cleans_up_the_trigger(workload, continuous, stop_fails):
    workload.continuous = continuous
    workload.cleanup_cpu_stress = Mock()
    stop = workload.wrk.delete_bhotelwrk_deployment if continuous else workload.wrk.stop_workload
    if stop_fails:
        stop.side_effect = RuntimeError("workload stop failed")
        with pytest.raises(RuntimeError, match="workload stop failed"):
            workload.stop()
    else:
        workload.stop()

    workload.cleanup_cpu_stress.assert_called_once_with(wait=True)
    if continuous:
        stop.assert_called_once_with(deployment_name=workload.deployment_name, namespace=workload.namespace)
    else:
        stop.assert_called_once_with(job_name=workload.job_name, namespace=workload.namespace)


def test_unrelated_blueprint_workloads_do_not_clean_up_the_cpu_trigger(workload):
    workload.CPU_containment = False
    workload.cleanup_cpu_stress = Mock()

    workload.stop()

    workload.cleanup_cpu_stress.assert_not_called()


@pytest.mark.parametrize(
    "module_name,class_name",
    [
        ("load_spike_rpc_retry_storm", "LoadSpikeRPCRetryStorm"),
        ("capacity_decrease_rpc_retry_storm", "CapacityDecreaseRPCRetryStorm"),
    ],
)
@pytest.mark.parametrize("failure", [None, "restore", "cleanup"])
def test_problem_recovery_cleans_up_without_stopping_the_required_traffic(
    monkeypatch, module_name, class_name, failure
):
    module = import_module(f"sregym.conductor.problems.{module_name}")
    problem = getattr(module, class_name).__new__(getattr(module, class_name))
    problem.namespace = "blueprint-hotel-reservation"
    problem.faulty_service = "rpc"
    problem.fault_injected = True
    problem.wrk = Mock()
    injector = Mock()
    monkeypatch.setattr(module, "VirtualizationFaultInjector", lambda **_: injector)
    core = Mock()
    apps = Mock()
    apps.list_namespaced_deployment.return_value = SimpleNamespace(items=[])
    monkeypatch.setattr(module.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(module.client, "AppsV1Api", lambda: apps)
    if failure == "restore":
        injector.recover_rpc_timeout_retries_misconfiguration.side_effect = RuntimeError("restore failed")
    elif failure == "cleanup":
        problem.wrk.cleanup_cpu_stress.side_effect = RuntimeError("cleanup failed")

    if failure:
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            problem.recover_fault()
        assert problem.fault_injected is True
    else:
        problem.recover_fault()
        assert problem.fault_injected is False

    problem.wrk.cleanup_cpu_stress.assert_called_once_with(wait=True)
    problem.wrk.stop.assert_not_called()


@pytest.mark.parametrize(
    "module_name,class_name",
    [
        ("load_spike_rpc_retry_storm", "LoadSpikeRPCRetryStorm"),
        ("capacity_decrease_rpc_retry_storm", "CapacityDecreaseRPCRetryStorm"),
    ],
)
def test_recovery_before_workload_creation_is_safe(monkeypatch, module_name, class_name):
    module = import_module(f"sregym.conductor.problems.{module_name}")
    problem = getattr(module, class_name).__new__(getattr(module, class_name))
    problem.namespace = "blueprint-hotel-reservation"
    problem.faulty_service = "rpc"
    monkeypatch.setattr(module, "VirtualizationFaultInjector", Mock())
    monkeypatch.setattr(module.client, "CoreV1Api", Mock())
    apps = Mock()
    apps.list_namespaced_deployment.return_value = SimpleNamespace(items=[])
    monkeypatch.setattr(module.client, "AppsV1Api", lambda: apps)

    problem.recover_fault()

    assert problem.fault_injected is False
