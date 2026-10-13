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
    result = FaultArtifact.load(path).run("inject.sh", "hotel")
    artifact = FaultArtifact.load(path)
    assert result.returncode == 0 and f"ns=hotel dir={artifact.workdir()}" in result.stdout


def test_scripts_can_keep_state(tmp_path):
    path = make(tmp_path, "s1", inject='echo saved > "$ARTIFACT_DIR/state"\n')
    (path / "recover.sh").write_text('cat "$ARTIFACT_DIR/state"\n')
    path.chmod(0o555)
    artifact = FaultArtifact.load(path)
    assert artifact.run("inject.sh", "ns").returncode == 0
    assert artifact.run("recover.sh", "ns").stdout.strip() == "saved"
    path.chmod(0o755)


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
