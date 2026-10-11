from types import SimpleNamespace

from sregym.conductor.problems.mongo_storage_faults import LatentSectorError, SilentDataCorruption


class FakeKubectl:
    def __init__(self):
        self.calls: list[str] = []
        pod = SimpleNamespace(
            spec=SimpleNamespace(
                node_name="kind-worker2",
                volumes=[
                    SimpleNamespace(persistent_volume_claim=None),
                    SimpleNamespace(persistent_volume_claim=SimpleNamespace(claim_name="geo-pvc")),
                ],
            )
        )
        self.core_v1_api = SimpleNamespace(
            list_namespaced_pod=lambda namespace, label_selector: SimpleNamespace(items=[pod]),
            read_namespaced_persistent_volume_claim=lambda name, namespace: SimpleNamespace(
                spec=SimpleNamespace(volume_name="pvc-123")
            ),
            read_persistent_volume=lambda name: SimpleNamespace(
                spec=SimpleNamespace(local=SimpleNamespace(path="/var/openebs/local/pvc-123"))
            ),
        )

    def exec_command_checked(self, command, timeout=None):
        self.calls.append(command)

    def create_namespace_if_not_exist(self, namespace):
        pass

    def run_node_script_pod(self, node_name, namespace, script, name_prefix):
        self.calls.append(f"pod {namespace} {node_name}: {script}")


def _inject(problem_class):
    # Skip __init__: it builds the real app and oracles.
    problem = object.__new__(problem_class)
    problem.kubectl = FakeKubectl()
    problem.namespace = "hotel-reservation"
    problem.deploy = "mongodb-geo"
    problem.inject_fault()
    return problem.kubectl.calls


def test_files_are_edited_on_the_volume_only_while_mongod_is_stopped():
    # mongod's clean shutdown rewrites the files, so editing them while it runs undoes the fault.
    calls = _inject(LatentSectorError)

    assert "--replicas=0" in calls[0]
    assert "--for=delete" in calls[1]
    assert calls[2].startswith("pod khaos kind-worker2: ")
    assert 'cd "/host/var/openebs/local/pvc-123"' in calls[2]
    assert "truncate -s 4096" in calls[2]
    assert "--replicas=1" in calls[3]


def test_silent_data_corruption_overwrites_instead_of_truncating():
    script = _inject(SilentDataCorruption)[2]

    assert "dd if=/dev/urandom" in script
    assert "truncate" not in script
