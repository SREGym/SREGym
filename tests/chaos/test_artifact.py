import json

import pytest

from sregym.chaos.artifact import FaultArtifact, discover

META = {
    "app": "hotel_reservation",
    "title": "Stale rate cache entries",
    "fault_layer": "data",
    "fault_primitive": "memcached poisoning",
    "root_cause_class": "stale-cache",
    "incident_class": "cache inconsistency",
}


@pytest.fixture(autouse=True)
def _workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("SREGYM_FAULT_WORKDIR", str(tmp_path / "work"))


def make(tmp_path, name="a1", meta=META, inject="kubectl -n $NAMESPACE rollout restart deploy/rate\n"):
    path = tmp_path / name
    path.mkdir()
    (path / "fault.json").write_text(json.dumps(meta))
    (path / "inject.sh").write_text(inject)
    (path / "recover.sh").write_text("true\n")
    (path / "mitigate.sh").write_text("true\n")
    return path


def test_valid_artifact(tmp_path):
    artifact = FaultArtifact.load(make(tmp_path))
    assert artifact.problems() == []
    assert artifact.leak_findings() == []
    assert artifact.problem_id == "gen_a1"


@pytest.mark.parametrize("value", [-1, True, "360", 3.5])
def test_invalid_recurrence_bound_is_reported(tmp_path, value):
    artifact = FaultArtifact.load(make(tmp_path, meta={**META, "recurrence_bound_s": value}))
    assert "recurrence_bound_s must be a nonnegative integer" in artifact.problems()


def test_missing_fields_and_scripts(tmp_path):
    path = make(tmp_path, meta={"app": "astronomy_shop"})
    (path / "mitigate.sh").unlink()
    problems = FaultArtifact.load(path).problems()
    assert "missing mitigate.sh" in problems
    assert any("unsupported app" in p for p in problems)
    assert any("fault_layer" in p for p in problems)


def test_leaks_ignore_comments(tmp_path):
    inject = "# inject the fault\nkubectl -n $NAMESPACE label deploy/rate chaos=on\n"
    findings = FaultArtifact.load(make(tmp_path, inject=inject)).leak_findings()
    assert len(findings) == 1 and "inject.sh:2" in findings[0]


def test_run_passes_namespace(tmp_path):
    path = make(tmp_path, inject='echo "ns=$NAMESPACE dir=$ARTIFACT_DIR"\n')
    workspace = FaultArtifact.load(path).workspace()
    result = workspace.run("inject.sh", "hotel")
    assert result.returncode == 0 and f"ns=hotel dir={workspace.path}" in result.stdout


def test_scripts_can_keep_state(tmp_path):
    path = make(tmp_path, "s1", inject='echo saved > "$ARTIFACT_DIR/state"\n')
    (path / "recover.sh").write_text('cat "$ARTIFACT_DIR/state"\n')
    path.chmod(0o555)
    workspace = FaultArtifact.load(path).workspace()
    assert workspace.run("inject.sh", "ns").returncode == 0
    assert workspace.run("recover.sh", "ns").stdout.strip() == "saved"
    workspace.close()
    assert not workspace.path.exists()
    path.chmod(0o755)


def test_run_preserves_explicit_kubeconfig(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".kube").mkdir(parents=True)
    (home / ".kube/config").touch()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KUBECONFIG", "/selected/config:/selected/overlay")
    artifact = FaultArtifact.load(make(tmp_path, inject='printf "%s" "$KUBECONFIG"'))

    assert artifact.workspace().run("inject.sh", "hotel").stdout == "/selected/config:/selected/overlay"


def test_run_keeps_kubectl_default_selection(tmp_path, monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    artifact = FaultArtifact.load(make(tmp_path, inject='printf "%s" "${KUBECONFIG-unset}"'))

    assert artifact.workspace().run("inject.sh", "hotel").stdout == "unset"


def test_attempts_copy_current_source_without_sharing_state(tmp_path):
    path = make(tmp_path, inject='echo old; touch "$ARTIFACT_DIR/state"')
    artifact = FaultArtifact.load(path)
    first = artifact.workspace()
    assert first.run("inject.sh", "ns").stdout.strip() == "old"
    (path / "inject.sh").write_text('echo new; test ! -e "$ARTIFACT_DIR/state"')
    second = artifact.workspace()

    assert first.path != second.path
    assert second.run("inject.sh", "ns").stdout.strip() == "new"
    assert second.run("inject.sh", "ns").returncode == 0
    assert (first.path / "state").exists()


def test_equal_names_in_different_roots_do_not_share_scripts(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    first = FaultArtifact.load(make(left, inject="echo left")).workspace()
    second = FaultArtifact.load(make(right, inject="echo right")).workspace()
    assert first.path != second.path
    assert second.run("inject.sh", "ns").stdout.strip() == "right"


def test_failed_recovery_retains_workspace(tmp_path):
    from sregym.conductor.problems.generated_problem import GeneratedProblem

    path = make(tmp_path, inject='echo saved > "$ARTIFACT_DIR/state"')
    (path / "recover.sh").write_text("exit 1")
    problem = GeneratedProblem.__new__(GeneratedProblem)
    problem.namespace = "ns"
    problem.workspace = FaultArtifact.load(path).workspace()
    problem.workspace.run("inject.sh", "ns")

    with pytest.raises(RuntimeError, match="recover.sh failed"):
        problem.recover_fault()
    assert (problem.workspace.path / "state").read_text().strip() == "saved"


def test_successful_recovery_removes_only_its_attempt_workspace(tmp_path):
    from sregym.conductor.problems.generated_problem import GeneratedProblem

    artifact = FaultArtifact.load(make(tmp_path))
    other = artifact.workspace()
    workspace = artifact.workspace()
    problem = GeneratedProblem.__new__(GeneratedProblem)
    problem.namespace = "ns"
    problem.workspace = workspace
    problem.fault_injected = True

    problem.recover_fault()
    assert problem.workspace is None
    assert not problem.fault_injected
    assert not workspace.path.exists()
    assert other.path.exists()
    problem.recover_fault()  # Cleanup is safe after a successful earlier recovery.


def test_recovery_before_injection_does_not_create_workspace():
    from sregym.conductor.problems.generated_problem import GeneratedProblem

    problem = GeneratedProblem.__new__(GeneratedProblem)
    problem.workspace = None
    problem.recover_fault()
    assert problem.workspace is None


def test_discover_extra_roots(tmp_path, monkeypatch):
    make(tmp_path, "b2")
    monkeypatch.setenv("SREGYM_GENERATED_DIRS", str(tmp_path))
    assert "gen_b2" in discover()


def test_notes_are_not_linted(tmp_path):
    path = make(tmp_path)
    (path / "validation.json").write_text('{"first_injection_checks": []}')
    (path / "design.md").write_text("The fault is injected into rate.")
    (path / "cm.yaml").write_text("metadata:\n  name: fault-config\n")
    findings = FaultArtifact.load(path).leak_findings()
    assert findings == ["cm.yaml:2: name: fault-config"]


def test_default_is_not_a_leak(tmp_path):
    inject = 'kubectl -n $NAMESPACE patch cm cfg -p "{\\"data\\":{\\"default\\":\\"1\\"}}"\n'
    assert FaultArtifact.load(make(tmp_path, inject=inject)).leak_findings() == []


def test_protected_strings_walk():
    from sregym.chaos.protected import _strings

    found = dict(_strings({"metadata": {"labels": {"chaos-run": "1"}}, "spec": {"args": ["--mode=default"]}}))
    assert ".metadata.labels.chaos-run" in found and found[".spec.args[0]"] == "--mode=default"
