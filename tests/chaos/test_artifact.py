import json

from sregym.chaos.artifact import FaultArtifact, discover

META = {
    "app": "hotel_reservation",
    "title": "Stale rate cache entries",
    "fault_layer": "data",
    "fault_primitive": "memcached poisoning",
    "root_cause_class": "stale-cache",
    "incident_class": "cache inconsistency",
}


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
    assert result.returncode == 0 and f"ns=hotel dir={path}" in result.stdout


def test_discover_extra_roots(tmp_path, monkeypatch):
    make(tmp_path, "b2")
    monkeypatch.setenv("SREGYM_GENERATED_DIRS", str(tmp_path))
    assert "gen_b2" in discover()
