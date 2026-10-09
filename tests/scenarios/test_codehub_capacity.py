"""Native allocation and private fail-closed capacity controls."""

import json
import os
import subprocess
import sys
import threading
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from sregym.conductor.scenarios.codehub_capacity import ALLOCATION_PROGRAM, GIB, CapacityMonitor, NativeStorageObserver


@pytest.mark.skipif(os.name != "posix", reason="Native directory-FD observations require Linux")
def test_native_allocations_distinguish_sparse_blocks_deduplicate_links_and_ignore_symlinks(tmp_path):
    root, outside = tmp_path / "owned", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "foreign").write_bytes(b"foreign" * 10000)
    sparse = root / "sparse"
    with sparse.open("wb") as file:
        file.truncate(64 * 1024**2)
    content = root / "data"
    content.write_bytes(b"x" * 16000)
    os.link(content, root / "hardlink")
    (root / "foreign-link").symlink_to(outside, target_is_directory=True)
    (root / "recovered.jsonl").write_bytes(b"retained operation\n" * 128)
    (root / "mysql-bin.000001").write_bytes(b"logged" * 128)
    result = subprocess.run(
        [sys.executable, "-c", ALLOCATION_PROGRAM],
        input=json.dumps({"owned": str(root)}),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    facts = json.loads(result.stdout)["owned"]
    assert facts["logical"] >= 64 * 1024**2 and facts["allocated"] < 1024**2
    assert facts["allocated"] == (root / "foreign-link").lstat().st_blocks * 512 + sum(
        path.stat().st_blocks * 512
        for path in (root, sparse, content, root / "recovered.jsonl", root / "mysql-bin.000001")
    )
    assert facts["temporary"] == (root / "recovered.jsonl").stat().st_blocks * 512
    assert facts["logs"] == (root / "mysql-bin.000001").stat().st_blocks * 512
    assert facts["free_inodes"] > 0
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    failed = subprocess.run(
        [sys.executable, "-c", ALLOCATION_PROGRAM],
        input=json.dumps({"alias": str(alias)}),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert failed.returncode != 0 and not failed.stdout


@pytest.mark.skipif(os.name != "posix", reason="Native directory-FD observations require Linux")
@pytest.mark.parametrize("replace_root", [False, True])
def test_native_complete_sample_retries_actual_unlink_but_refuses_root_replacement(tmp_path, replace_root):
    root = tmp_path / "owned"
    root.mkdir()
    (root / "retained").write_bytes(b"retained allocation" * 1024)
    raced = root / "rotating.log"
    raced.write_bytes(b"old rotated content")
    # Produce an actual ENOENT between directory enumeration and entry.stat.
    # The remaining allocation must still be observed after a complete retry.
    injection = r"""
original_scandir=os.scandir
class Entry:
 def __init__(self,entry): self.entry=entry;self.name=entry.name
 def stat(self,**kwargs):
  if self.name=='rotating.log' and os.path.exists(raced):
   os.unlink(raced)
   if replace_root:
    os.rename(root,root+'.old')
    os.symlink(root+'.old',root,target_is_directory=True)
  return self.entry.stat(**kwargs)
class Scan:
 def __init__(self,fd): self.scan=original_scandir(fd)
 def __enter__(self): return (Entry(entry) for entry in self.scan)
 def __exit__(self,*args): self.scan.close()
os.scandir=Scan
"""
    source = (
        "import os\n" + repr(str(root)).join(["root=", "\n"]) + f"raced={str(raced)!r}\nreplace_root={replace_root!r}\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", source + injection + ALLOCATION_PROGRAM],
        input=json.dumps({"owned": str(root), "filesystem:trusted": str(tmp_path)}),
        text=True,
        capture_output=True,
        timeout=10,
    )
    if replace_root:
        assert result.returncode != 0 and not result.stdout
    else:
        assert result.returncode == 0, result.stderr
        facts = json.loads(result.stdout)
        assert facts["owned"]["allocated"] == root.stat().st_blocks * 512 + (root / "retained").stat().st_blocks * 512
        assert facts["filesystem:trusted"]["allocated"] == 0
        assert facts["filesystem:trusted"]["free_inodes"] > 0


def sample():
    return {
        "at": time.monotonic(),
        "memory_bytes": 2 * GIB,
        "cpu_nanoseconds": 100,
        "oom": False,
        "node_allocated_bytes": 10 * GIB,
        "owner_allocated_bytes": GIB,
        "stores": {"sql": GIB},
        "native_roots": {"owner": {"free_inodes": 100000}, "filesystem:trusted": {"free_inodes": 100000}},
        "capacities": {
            name: {"available_disk_gib": 100, "available_memory_gib": 64} for name in ("workload", "trusted", "owner")
        },
    }


@pytest.mark.parametrize("failure", ["disk", "inodes", "trusted_inodes", "memory", "growth", "oom", "observer"])
def test_runtime_capacity_failure_stops_owner_and_never_claims_zero_or_a_verdict(tmp_path, failure):
    current, cancelled, closed = [sample()], [], []

    def observe():
        if failure == "observer" and len(cancelled) == 0 and current[0] is None:
            raise RuntimeError("Captured allocation unavailable")
        return deepcopy(current[0])

    observer = SimpleNamespace(sample=observe, close=lambda: closed.append(True))
    monitor = CapacityMonitor(
        observer,
        tmp_path / "private.jsonl",
        cancel=lambda: cancelled.append(True),
        disk_budget=12 * GIB,
        reserve_gib=8,
        interval=0.01,
    )
    monitor.start()
    try:
        if failure == "disk":
            current[0]["capacities"]["workload"]["available_disk_gib"] = 1
        elif failure == "inodes":
            current[0]["native_roots"]["owner"]["free_inodes"] = 1
        elif failure == "trusted_inodes":
            current[0]["native_roots"]["filesystem:trusted"]["free_inodes"] = 1
        elif failure == "memory":
            current[0]["capacities"]["owner"]["available_memory_gib"] = 1
        elif failure == "growth":
            current[0]["node_allocated_bytes"] += 3 * GIB
        elif failure == "oom":
            current[0]["oom"] = True
        else:
            current[0] = None
        deadline = time.monotonic() + 2
        while not cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cancelled and monitor.error is not None
        with pytest.raises(RuntimeError, match="unavailable"):
            monitor.assert_available()
        retained = [json.loads(line) for line in (tmp_path / "private.jsonl").read_text().splitlines()]
        assert retained and retained[0]["node_allocated_bytes"] == 10 * GIB
        assert all("success" not in row for row in retained)
    finally:
        monitor.stop()
    assert closed == [True]


def test_fatal_workload_capacity_sample_survives_cancellation_and_close(tmp_path):
    current, cancelled = sample(), []
    path = tmp_path / "private.jsonl"
    observer = SimpleNamespace(sample=lambda: deepcopy(current), close=lambda: None)
    monitor = CapacityMonitor(
        observer, path, cancel=lambda: cancelled.append(True), disk_budget=20 * GIB, reserve_gib=8
    )
    monitor.start()
    current["oom"] = True
    with pytest.raises(RuntimeError, match="capacity reserve"):
        monitor.observe()
    monitor.stop()
    with pytest.raises(RuntimeError, match="observation failed"):
        monitor.assert_completed()
    fatal = json.loads(path.read_text().splitlines()[-1])
    assert fatal["oom"] is True
    assert fatal["capacity_failure_reasons"] == ["unattributed_workload_oom"]
    assert fatal["native_roots"] == current["native_roots"]
    assert "success" not in fatal and "failure_class" not in fatal
    assert path.stat().st_mode & 0o777 == 0o600


def test_fatal_sample_is_unavailable_before_persistence_and_survives_concurrent_stop(tmp_path, monkeypatch):
    from sregym.conductor.scenarios import codehub_capacity as module

    current, cancelled = sample(), []
    persistence_entered, release_persistence, observer_cancel = (threading.Event() for _ in range(3))
    real_fsync = os.fsync

    def blocked_fsync(descriptor):
        persistence_entered.set()
        assert release_persistence.wait(3)
        real_fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", blocked_fsync)
    monitor = CapacityMonitor(
        SimpleNamespace(sample=lambda: deepcopy(current), cancel=observer_cancel, close=lambda: None),
        tmp_path / "fatal.jsonl",
        cancel=lambda: cancelled.append(True),
        disk_budget=20 * GIB,
        reserve_gib=8,
        interval=0.01,
    )
    monitor.start()
    current["oom"] = True
    stopper = None
    try:
        assert persistence_entered.wait(2)
        with pytest.raises(RuntimeError, match="unavailable"):
            monitor.assert_available()
        stopper = threading.Thread(target=monitor.stop)
        stopper.start()
        assert observer_cancel.wait(2)
    finally:
        release_persistence.set()
        if stopper is not None:
            stopper.join(3)
        else:
            monitor.stop()
    assert stopper is not None and not stopper.is_alive() and not monitor.thread.is_alive()
    assert cancelled and monitor.error is not None
    with pytest.raises(RuntimeError, match="observation failed"):
        monitor.assert_completed()
    fatal = json.loads(monitor.path.read_text().splitlines()[-1])
    assert fatal["capacity_failure_reasons"] == ["unattributed_workload_oom"]
    assert "failure_class" not in fatal


def test_restore_checks_fresh_capacity_before_mutating_work(tmp_path):
    current, calls = sample(), []
    observer = SimpleNamespace(sample=lambda: (calls.append("sample") or deepcopy(current)), close=lambda: None)
    monitor = CapacityMonitor(
        observer, tmp_path / "private.jsonl", cancel=lambda: None, disk_budget=20 * GIB, reserve_gib=8
    )
    monitor.start()
    try:
        current["capacities"]["owner"]["available_disk_gib"] = 1
        with pytest.raises(RuntimeError, match="reserve is exhausted"):
            monitor.reserve_restore()
        assert calls == ["sample", "sample"]
    finally:
        monitor.stop()


@pytest.mark.parametrize("used_gib,allowed", [(10, True), (19, False)])
def test_restore_projected_copy_is_admitted_against_absolute_budget_even_with_free_disk(tmp_path, used_gib, allowed):
    current = sample()
    current["node_allocated_bytes"] = used_gib * GIB
    observer = SimpleNamespace(sample=lambda: deepcopy(current), close=lambda: None)
    monitor = CapacityMonitor(
        observer, tmp_path / "private.jsonl", cancel=lambda: None, disk_budget=21 * GIB, reserve_gib=8
    )
    monitor.start()
    try:
        if allowed:
            monitor.reserve_restore(additional_bytes=2 * GIB)
        else:
            with pytest.raises(RuntimeError, match="Projected recovery allocation"):
                monitor.reserve_restore(additional_bytes=2 * GIB)
    finally:
        monitor.stop()


def test_actual_chart_claim_names_attribute_artifacts_before_delivery():
    assert NativeStorageObserver._store_role("artifacts-delivery-0") == "artifact"
    assert NativeStorageObserver._store_role("data-delivery-0") == "delivery"
    assert NativeStorageObserver._store_role("data-mysql-g0-writer-0") == "sql"


@pytest.mark.parametrize("changed_engine", [False, True])
def test_predeployment_kernel_capture_closes_native_docker_client_and_checks_engine(monkeypatch, changed_engine):
    from sregym.conductor.scenarios import codehub_capacity as module

    closed, calls = [], []
    identity = "a" * 64
    node = SimpleNamespace(
        id=identity,
        attrs={
            "State": {"Running": True, "Pid": 123},
            "Config": {"Labels": {"io.x-k8s.kind.cluster": "owned"}},
            "HostConfig": {"Memory": 10 * GIB, "MemorySwap": 10 * GIB, "NanoCpus": 4 * 10**9, "PidsLimit": 8192},
        },
    )
    client = SimpleNamespace(
        info=lambda: {"ID": "changed" if changed_engine else "captured"},
        containers=SimpleNamespace(get=lambda _: node),
        close=lambda: closed.append(True),
    )
    monkeypatch.setenv("DOCKER_HOST", "unix:///qualified.sock")
    monkeypatch.setattr(module.docker, "DockerClient", lambda **_: client)
    monkeypatch.setattr(
        module,
        "native_kernel",
        lambda nodes, **_: (
            calls.append(nodes)
            or {
                identity: {
                    "uid": 20042,
                    "memory_limit": 10 * GIB,
                    "memory_swap_limit": 0,
                    "cpu_quota": 400000,
                    "cpu_period": 100000,
                    "pids_limit": 8192,
                }
            }
        ),
    )
    boundary = {"workload_engine": "captured", "workload_uid": 20042, "nodes": ("owned-worker",)}
    if changed_engine:
        with pytest.raises(RuntimeError, match="engine identity"):
            module.capture_kernel_baseline(boundary)
        assert not calls
    else:
        assert module.capture_kernel_baseline(boundary)[identity]["declared_limits"] == {
            "Memory": 10 * GIB,
            "MemorySwap": 10 * GIB,
            "NanoCpus": 4 * 10**9,
            "PidsLimit": 8192,
        }
        assert calls == [{identity: 123}]
    assert closed == [True]


def test_slow_successful_observation_is_cancelled_and_owned_client_closes(tmp_path):
    import threading

    entered, cancel = threading.Event(), threading.Event()
    calls, closed = [], []

    def observe():
        calls.append(True)
        if len(calls) > 1:
            entered.set()
            assert cancel.wait(2)
            raise RuntimeError("Observation cancelled")
        return sample()

    observer = SimpleNamespace(sample=observe, cancel=cancel, close=lambda: closed.append(True))
    monitor = CapacityMonitor(
        observer,
        tmp_path / "private.jsonl",
        cancel=lambda: pytest.fail("Normal cleanup is not an owner failure"),
        disk_budget=20 * GIB,
        reserve_gib=8,
        interval=0.01,
    )
    monitor.start()
    assert entered.wait(2)
    started = time.monotonic()
    monitor.stop()
    assert time.monotonic() - started < 1 and not monitor.thread.is_alive() and closed == [True]
    assert monitor.error is None
    monitor.assert_completed()


@pytest.mark.parametrize("node_gib,allowed", [(210, True), (219, False)])
def test_large_budget_charges_private_verifier_reserve_before_scratch_creation(tmp_path, node_gib, allowed):
    current = sample()
    current["node_allocated_bytes"] = node_gib * GIB
    observer = SimpleNamespace(sample=lambda: deepcopy(current), close=lambda: None)
    monitor = CapacityMonitor(
        observer,
        tmp_path / "resources.jsonl",
        cancel=lambda: None,
        disk_budget=220 * GIB,
        reserve_gib=8,
        verifier_reserve_bytes=8 * GIB,
    )
    try:
        if allowed:
            monitor.start()
            assert monitor.latest["combined_allocated_and_reserved_bytes"] == 219 * GIB
            with pytest.raises(RuntimeError, match="Projected recovery"):
                monitor.reserve_restore(additional_bytes=2 * GIB)
        else:
            with pytest.raises(RuntimeError, match="reserve is exhausted"):
                monitor.start()
    finally:
        monitor.stop()


@pytest.mark.parametrize("source_gib,allowed", [(2, True), (9, False)])
def test_copy_admission_uses_selected_pvc_not_all_unaffected_sql(tmp_path, source_gib, allowed):
    from sregym.conductor.scenarios.codehub_reference_repair import DatabaseReferenceRepair

    current = sample()
    current["store_allocations"] = {
        "codehub-region-b/data-mysql-g0-candidate-0": source_gib * GIB,
        "unaffected/data-mysql-g1-writer-0": 100 * GIB,
    }
    monitor = CapacityMonitor(
        SimpleNamespace(sample=lambda: deepcopy(current), close=lambda: None),
        tmp_path / "resources.jsonl",
        cancel=lambda: None,
        disk_budget=19 * GIB,
        reserve_gib=8,
    )
    monitor.start()
    repair = DatabaseReferenceRepair(
        SimpleNamespace(
            _capacity_monitor=monitor, regions=(SimpleNamespace(name="region-b", namespace="codehub-region-b"),)
        ),
        tmp_path,
    )
    member = SimpleNamespace(region="region-b", origin="mysql://mysql-g0-candidate.codehub-region-b.svc:3306")
    try:
        if allowed:
            repair._reserve_copy(member)
        else:
            with pytest.raises(RuntimeError, match="Projected recovery"):
                repair._reserve_copy(member)
    finally:
        monitor.stop()


@pytest.mark.parametrize(
    "changed", [None, "kernel-memory", "kernel-cpu", "kernel-pids", "docker-memory", "docker-swap", "kernel-swap"]
)
def test_native_limits_require_exact_docker_kernel_and_frozen_agreement(changed):
    from sregym.conductor.scenarios.codehub_capacity import _validate_node_limits

    declared = {"Memory": 10 * GIB, "MemorySwap": 10 * GIB, "NanoCpus": 4 * 10**9, "PidsLimit": 8192}
    facts = {
        "memory_limit": 10 * GIB, "memory_swap_limit": 0,
        "cpu_quota": 400000, "cpu_period": 100000, "pids_limit": 8192,
    }
    baseline = deepcopy(facts) | {"declared_limits": deepcopy(declared)}
    if changed == "docker-memory":
        declared["Memory"] += GIB
    elif changed == "docker-swap":
        declared["MemorySwap"] += GIB
    elif changed == "kernel-swap":
        facts["memory_swap_limit"] += 1
    elif changed:
        key = {"kernel-memory": "memory_limit", "kernel-cpu": "cpu_quota", "kernel-pids": "pids_limit"}[changed]
        facts[key] += 1
    if changed:
        with pytest.raises(RuntimeError, match="unenforced"):
            _validate_node_limits(facts, declared, baseline=baseline)
    else:
        _validate_node_limits(facts, declared, baseline=baseline)


def native_large_fixture():
    names = ["owned-control-plane", *(f"owned-worker-{number}" for number in range(9))]
    baseline = {}
    for number, name in enumerate(names):
        memory, cpu = (6, 3) if number == 0 else (20, 5)
        baseline[str(number)] = {
            "node_name": name,
            "uid": 20042,
            "memory_limit": memory * GIB,
            "memory_swap_limit": 0,
            "memory_current": 100 * GIB,
            "cpu_quota": cpu * 100000,
            "cpu_period": 100000,
            "pids_limit": 8192,
            "declared_limits": {
                "Memory": memory * GIB, "MemorySwap": memory * GIB,
                "NanoCpus": cpu * 10**9, "PidsLimit": 8192,
            },
            "ancestors": [{"path": "/user.slice", "device": 1, "inode": 2, "memory_limit": None}],
        }
    return baseline, {"nodes": names, "workload_uid": 20042}


@pytest.mark.parametrize("available,allowed", [(217, False), (218, True), (228, True)])
def test_native_large_reserves_outer_ceilings_without_double_counting_current_usage(available, allowed):
    from sregym.conductor.scenarios.codehub_capacity import admit_native_memory
    from sregym.conductor.scenarios.database_recovery import TIERS, HostCapacity

    capacity = HostCapacity(64, available, 1000)
    with pytest.raises(ValueError, match="memory headroom"):
        capacity.admit(TIERS["large"])
    baseline, boundary = native_large_fixture()
    if allowed:
        facts = admit_native_memory(capacity, TIERS["large"], foreign_reserve_gib=8, baseline=baseline, boundary=boundary)
        assert facts["phase"] == "actual" and facts["required_memory_gib"] == 218
        assert facts["current_usage_credit_gib"] == 0 and facts["node_memory_ceiling_gib"] == 186
    else:
        with pytest.raises(ValueError, match="reserves exceed available"):
            admit_native_memory(capacity, TIERS["large"], foreign_reserve_gib=8, baseline=baseline, boundary=boundary)


@pytest.mark.parametrize("changed", ["missing", "wrong-name", "uid", "memory", "swap", "ancestor", "foreign", "cpu", "disk"])
def test_native_large_refuses_incomplete_enforcement_or_insufficient_reserves(changed):
    from sregym.conductor.scenarios.codehub_capacity import admit_native_memory
    from sregym.conductor.scenarios.database_recovery import TIERS, HostCapacity

    baseline, boundary = native_large_fixture()
    capacity, foreign = HostCapacity(64, 228, 1000), 8
    if changed == "missing":
        baseline.pop("9")
    elif changed == "wrong-name":
        baseline["1"]["node_name"] = "foreign-worker"
    elif changed == "uid":
        baseline["1"]["uid"] = 20041
    elif changed == "memory":
        baseline["1"]["memory_limit"] += GIB
    elif changed == "swap":
        baseline["1"]["memory_swap_limit"] = 1
    elif changed == "ancestor":
        for facts in baseline.values():
            facts["ancestors"][0]["memory_limit"] = 186 * GIB
    elif changed == "foreign":
        foreign = 19
    elif changed == "cpu":
        capacity = HostCapacity(63, 228, 1000)
    else:
        capacity = HostCapacity(64, 228, 300)
    with pytest.raises((RuntimeError, ValueError)):
        admit_native_memory(capacity, TIERS["large"], foreign_reserve_gib=foreign, baseline=baseline, boundary=boundary)


def test_native_large_prospective_reservation_does_not_claim_actual_enforcement():
    from sregym.conductor.scenarios.codehub_capacity import admit_native_memory
    from sregym.conductor.scenarios.database_recovery import TIERS, HostCapacity

    result = admit_native_memory(HostCapacity(64, 228, 1000), TIERS["large"], foreign_reserve_gib=8)
    assert result["phase"] == "prospective"
    baseline, boundary = native_large_fixture()
    for facts in baseline.values():
        facts["ancestors"][0]["memory_limit"] = 187 * GIB
    result = admit_native_memory(
        HostCapacity(64, 228, 1000), TIERS["large"], foreign_reserve_gib=8, baseline=baseline, boundary=boundary
    )
    assert result["phase"] == "actual"


@pytest.mark.parametrize("init_sidecar,over_limit", [(False, False), (True, False), (True, True)])
def test_application_quota_uses_sequential_init_and_live_sidecar_limits(init_sidecar, over_limit):
    quota = SimpleNamespace(
        metadata=SimpleNamespace(uid="quota"), spec=SimpleNamespace(hard={"limits.cpu": "6", "limits.memory": "6Gi"})
    )

    def container(name, limit, sidecar=False):
        return SimpleNamespace(
            name=name,
            restart_policy="Always" if sidecar else None,
            resources=SimpleNamespace(
                limits={"cpu": str(limit), "memory": f"{limit}Gi"}, requests={"cpu": "1", "memory": "1Gi"}
            ),
        )

    initial = [container("init-one", 4, init_sidecar), container("init-two", 2 if init_sidecar else 4)]
    pod = SimpleNamespace(
        metadata=SimpleNamespace(uid="pod"),
        status=SimpleNamespace(phase="Running"),
        spec=SimpleNamespace(
            node_name="owned",
            containers=[container("api", 3 if over_limit else 2)],
            init_containers=initial,
            overhead={},
        ),
    )
    core = SimpleNamespace(
        read_namespaced_resource_quota=lambda *a, **k: quota,
        list_namespaced_pod=lambda *a, **k: SimpleNamespace(items=[pod]),
    )
    observer = object.__new__(NativeStorageObserver)
    observer.app = SimpleNamespace(_client=lambda: SimpleNamespace(core_v1_api=core))
    observer.nodes = {"owned": "id"}
    observer.quotas = {"region": ("quota", quota.spec.hard)}
    observer._budget = lambda deadline: 1
    if over_limit:
        with pytest.raises(RuntimeError, match="exceed captured quota"):
            observer._application_resources(time.monotonic() + 5)
    else:
        facts = observer._application_resources(time.monotonic() + 5)["region"]
        assert facts["cpu_limit_nanocores"] == (6 if init_sidecar else 4) * 10**9


def test_capacity_history_rotates_owned_logs_without_losing_earlier_peak(tmp_path):
    current = sample()
    monitor = CapacityMonitor(
        SimpleNamespace(sample=lambda: deepcopy(current), close=lambda: None),
        tmp_path / "resources.jsonl",
        cancel=lambda: None,
        disk_budget=20 * GIB,
        reserve_gib=8,
    )
    monitor.start()
    try:
        with monitor.path.open("ab") as file:
            file.truncate(33 * 1024**2)
        current["memory_bytes"] = GIB
        monitor.observe()
        assert monitor.path.with_name(monitor.path.name + ".1").exists()
        row = json.loads(monitor.path.read_text())
        assert row["campaign_sampled_peak_memory_bytes"] == 2 * GIB
        assert row["campaign_peak_allocated_and_reserved_bytes"] == 11 * GIB
        assert monitor.error is None
    finally:
        monitor.stop()
