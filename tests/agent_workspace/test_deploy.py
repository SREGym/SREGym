import json

from sregym.agent_workspace import deploy


def _workspace(tmp_path, *, proposed: str, live: str | None):
    workspace = tmp_path / "workspace"
    deploy_dir = workspace / ".deploy"
    deploy_dir.mkdir(parents=True)
    (workspace / "recommendation_server.py").write_text(proposed, encoding="utf-8")
    (deploy_dir / "manifest.json").write_text(
        json.dumps(
            {
                "problem_id": "Example",
                "namespace": "astronomy-shop",
                "files": [
                    {
                        "workspace_path": "recommendation_server.py",
                        "pod_path": "/app/recommendation_server.py",
                        "deployment": "recommendation",
                        "configmap_name": "recommendation-src-override",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return workspace, live


def test_deploy_dry_run_does_not_patch(monkeypatch, tmp_path, capsys):
    workspace, _ = _workspace(tmp_path, proposed="fixed\n", live="buggy\n")
    commands = []

    def fake_run(cmd, *, check=True, stdin=None):
        commands.append(cmd)
        if cmd[:3] == ["kubectl", "get", "configmap"]:
            return json.dumps({"data": {"recommendation_server.py": "buggy\n"}})
        return ""

    monkeypatch.setattr(deploy, "_run", fake_run)

    assert deploy.main(["--workspace", str(workspace), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would patch recommendation-src-override" in out
    assert "dry-run" in out
    assert not any("patch" in cmd for cmd in commands)
    assert not any("rollout" in cmd for cmd in commands)


def test_deploy_patches_changed_file_and_rolls_out(monkeypatch, tmp_path):
    workspace, _ = _workspace(tmp_path, proposed="fixed\n", live="buggy\n")
    commands = []
    pod_reads = 0

    def fake_run(cmd, *, check=True, stdin=None):
        nonlocal pod_reads
        commands.append(cmd)
        if cmd[:3] == ["kubectl", "get", "configmap"]:
            return json.dumps({"data": {"recommendation_server.py": "buggy\n"}})
        if cmd[:3] == ["kubectl", "get", "deployment"]:
            return json.dumps(
                {"spec": {"selector": {"matchLabels": {"app": "recommendation"}}}}
            )
        if cmd[:3] == ["kubectl", "get", "pods"]:
            pod_reads += 1
            old_pod = {
                "metadata": {
                    "name": "recommendation-old",
                    "deletionTimestamp": "2026-09-12T00:00:00Z",
                }
            }
            current_pod = {"metadata": {"name": "recommendation-current"}}
            return json.dumps(
                {"items": [current_pod, old_pod] if pod_reads == 1 else [current_pod]}
            )
        return ""

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)

    assert deploy.main(["--workspace", str(workspace)]) == 0
    assert any(cmd[:2] == ["kubectl", "patch"] for cmd in commands)
    assert any(cmd[:3] == ["kubectl", "rollout", "restart"] for cmd in commands)
    assert any(cmd[:3] == ["kubectl", "rollout", "status"] for cmd in commands)
    assert pod_reads == 2


def test_deploy_skips_unchanged_file(monkeypatch, tmp_path, capsys):
    workspace, _ = _workspace(tmp_path, proposed="same\n", live="same\n")

    def fake_run(cmd, *, check=True, stdin=None):
        if cmd[:3] == ["kubectl", "get", "configmap"]:
            return json.dumps({"data": {"recommendation_server.py": "same\n"}})
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(deploy, "_run", fake_run)

    assert deploy.main(["--workspace", str(workspace)]) == 0
    assert "Nothing to deploy" in capsys.readouterr().out
