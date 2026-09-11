import copy
import json
import shlex
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.generators.fault import inject_operator


@pytest.fixture
def operator(monkeypatch, tmp_path):
    original = {
        "apiVersion": "pingcap.com/v1alpha1",
        "kind": "TidbCluster",
        "metadata": {"name": "basic", "namespace": "tidb-cluster", "uid": "cluster-resource-1"},
        "spec": {
            "version": "v8.1.0",
            "pvReclaimPolicy": "Retain",
            "pd": {"baseImage": "custom/pd", "replicas": 1, "storageClassName": "local"},
            "tikv": {"baseImage": "custom/tikv", "replicas": 1, "config": {"raftdb": {"max-open-files": 256}}},
            "tidb": {"baseImage": "custom/tidb", "replicas": 1, "config": {"log": {"level": "info"}}},
        },
    }
    state = SimpleNamespace(cluster=copy.deepcopy(original), original=original, fail_apply=False)
    kube = Mock()
    kube.core_v1_api.read_namespace.return_value.metadata.uid = "namespace-1"

    def execute(command, input_data=None):
        if command.startswith("kubectl get tidbcluster"):
            return json.dumps(state.cluster) if state.cluster is not None else ""
        if command.startswith("kubectl patch"):
            args = shlex.split(command)
            patch = json.loads(args[args.index("-p") + 1])
            if "--type=merge" in args:
                for component, values in patch["spec"].items():
                    state.cluster["spec"][component].update(values)
            else:
                state.cluster["spec"] = patch[0]["value"]
        if command.startswith("kubectl delete tidbcluster"):
            state.cluster = None
        if command.startswith("kubectl apply"):
            if state.fail_apply:
                raise RuntimeError("apply failed")
            state.cluster = json.loads(input_data)
            state.cluster["metadata"]["uid"] = "restored-resource"
        return "ok"

    kube.exec_command_checked.side_effect = execute
    monkeypatch.setattr(inject_operator, "KubeCtl", Mock(return_value=kube))
    monkeypatch.setattr(inject_operator, "RECOVERY_STATE_DIR", tmp_path)
    state.kube = kube
    state.injector = inject_operator.K8SOperatorFaultInjector("tidb-cluster")
    return state


@pytest.mark.parametrize(
    "fault,component,field",
    [
        ("overload_replicas", "tidb", "replicas"),
        ("invalid_affinity_toleration", "tidb", "tolerations"),
        ("security_context_fault", "tidb", "podSecurityContext"),
        ("wrong_update_strategy", "tidb", "statefulSetUpdateStrategy"),
        ("non_existent_storage", "pd", "storageClassName"),
    ],
)
def test_operator_faults_only_change_the_intended_field(operator, fault, component, field):
    getattr(operator.injector, f"inject_{fault}")()
    changed = copy.deepcopy(operator.cluster["spec"])
    if field in operator.original["spec"][component]:
        changed[component][field] = operator.original["spec"][component][field]
    else:
        del changed[component][field]
    assert changed == operator.original["spec"]
    assert operator.cluster["spec"] != operator.original["spec"]


def test_repeated_injection_and_new_injector_restore_the_original_images(operator):
    operator.injector.inject_wrong_update_strategy()
    operator.injector.inject_wrong_update_strategy()
    restored = inject_operator.K8SOperatorFaultInjector("tidb-cluster")
    restored.recover_wrong_update_strategy()
    assert operator.cluster["spec"] == operator.original["spec"]
    assert not list(inject_operator.RECOVERY_STATE_DIR.glob("*.json"))
    commands = [call.args[0] for call in operator.kube.exec_command_checked.call_args_list]
    assert not any("delete tidbcluster" in command or "https://" in command for command in commands)


def test_recovery_refuses_a_recreated_cluster(operator):
    operator.injector.inject_security_context_fault()
    operator.cluster["metadata"]["uid"] = "different-resource"
    with pytest.raises(RuntimeError, match="recreated"):
        operator.injector.recover_security_context_fault()
    assert list(inject_operator.RECOVERY_STATE_DIR.glob("*.json"))


def test_recovery_without_a_snapshot_does_not_install_a_generic_image(operator):
    with pytest.raises(RuntimeError, match="Original TiDB configuration is missing"):
        operator.injector.recover_wrong_update_strategy()
    operator.kube.exec_command_checked.assert_not_called()


def test_storage_recovery_can_resume_after_failed_recreation(operator):
    operator.injector.inject_non_existent_storage()
    operator.fail_apply = True
    with pytest.raises(RuntimeError, match="apply failed"):
        operator.injector.recover_non_existent_storage()
    assert operator.cluster is None
    assert list(inject_operator.RECOVERY_STATE_DIR.glob("*.json"))
    operator.fail_apply = False
    operator.injector.recover_non_existent_storage()
    assert operator.cluster["spec"] == operator.original["spec"]
    assert not list(inject_operator.RECOVERY_STATE_DIR.glob("*.json"))


def test_recovery_state_is_scoped_to_the_namespace_uid(operator):
    operator.injector.inject_wrong_update_strategy()
    operator.kube.core_v1_api.read_namespace.return_value.metadata.uid = "another-namespace"
    with pytest.raises(RuntimeError, match="Original TiDB configuration is missing"):
        operator.injector.recover_wrong_update_strategy()
