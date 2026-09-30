"""Guard complete build-time packages and fail-closed action registration."""

import json
import os
import runpy
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FLIGHT = ROOT / "SREGym-applications/flight-ticket"
DEPLOY = FLIGHT / "deploy_ow_actions"
PACKAGER = runpy.run_path(str(DEPLOY / "package_actions.py"))
MEMBERS = ("__main__.py", "virtualenv/bin/activate_this.py", "virtualenv/lib/python3.6/site-packages/redis/__init__.py")


@pytest.fixture
def deployment(tmp_path):
    script = tmp_path / "deploy_ow_actions.sh"
    shutil.copyfile(DEPLOY / script.name, script)
    for action in (DEPLOY / "actions").iterdir():
        if action.is_dir():
            package = tmp_path / "actions" / action.name / "function.zip"
            package.parent.mkdir(parents=True)
            with zipfile.ZipFile(package, "w") as archive:
                for member in MEMBERS:
                    archive.writestr(member, "# fixture\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wsk = bin_dir / "wsk"
    wsk.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        'with open(os.environ["CALL_LOG"], "a") as stream:\n'
        '    stream.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        'sys.exit(1 if os.environ.get("FAIL_AT") in sys.argv[1:] else 0)\n'
    )
    wsk.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "CALL_LOG": str(tmp_path / "calls.jsonl"),
        "PYTHON_RUNTIME_IMAGE": "registry.test/python-runtime:release",
        "WSK_API_HOST": "http://example.test",
        "WSK_AUTH_KEY": "test",
        "REDIS_HOST": "redis",
        "REDIS_PORT": "6379",
    }
    return script, env


def calls(env):
    path = Path(env["CALL_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run_deployment(deployment):
    script, env = deployment
    return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)


def test_registers_all_prebuilt_actions_with_matching_runtime(deployment):
    assert run_deployment(deployment).returncode == 0
    commands = calls(deployment[1])
    actions = [cmd for cmd in commands if "--docker" in cmd]
    assert len(actions) == 14
    assert all(cmd[:3] == ["-i", "action", "update"] for cmd in actions)
    assert all(cmd[cmd.index("--docker") + 1] == deployment[1]["PYTHON_RUNTIME_IMAGE"] for cmd in actions)
    assert not any("delete" in cmd or "list" in cmd for cmd in commands)


@pytest.mark.parametrize("missing", ["archive", "corrupt", *MEMBERS])
def test_incomplete_artifact_aborts_before_any_api_call(deployment, missing):
    archive = deployment[0].parent / "actions/SeatService/function.zip"
    if missing == "archive":
        archive.unlink()
    elif missing == "corrupt":
        archive.write_bytes(b"not a zip")
    else:
        with zipfile.ZipFile(archive, "w") as zipped:
            for member in MEMBERS:
                if member != missing:
                    zipped.writestr(member, "# fixture\n")
    assert run_deployment(deployment).returncode != 0
    assert calls(deployment[1]) == []


@pytest.mark.parametrize("failure", ["property", "seat-service"])
def test_api_failure_stops_deployment(deployment, failure):
    deployment[1]["FAIL_AT"] = failure
    assert run_deployment(deployment).returncode != 0
    commands = calls(deployment[1])
    assert failure in commands[-1]
    assert not any("save-order-info" in cmd for cmd in commands)


@pytest.mark.parametrize(
    "variable", ["PYTHON_RUNTIME_IMAGE", "WSK_API_HOST", "WSK_AUTH_KEY", "REDIS_HOST", "REDIS_PORT"]
)
def test_missing_configuration_aborts_before_any_api_call(deployment, variable):
    deployment[1].pop(variable)
    assert run_deployment(deployment).returncode != 0
    assert calls(deployment[1]) == []


def test_build_failure_does_not_produce_action_archives(tmp_path, monkeypatch):
    action = tmp_path / "source/Example"
    action.mkdir(parents=True)
    (action / "__main__.py").write_text("def main(args): return args\n")
    (action / "requirements.txt").write_text("redis\n")
    invoked = []

    def fail_install(command, *, check):
        assert check is True
        invoked.append(command)
        if "pip" in command:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", fail_install)
    with pytest.raises(subprocess.CalledProcessError):
        PACKAGER["package_actions"](action.parent, tmp_path / "out", DEPLOY / "requirements.lock.txt")
    assert len(invoked) == 2
    assert not (tmp_path / "out").exists()


def test_zip_contains_virtualenv_files_not_symlinks_or_old_artifacts(tmp_path):
    source, environment = tmp_path / "source", tmp_path / "virtualenv"
    source.mkdir()
    (source / "__main__.py").write_text("def main(args): return args\n")
    (source / "function.zip").write_bytes(b"stale")
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python-real").write_bytes(b"native executable")
    (environment / "bin/python").symlink_to("python-real")
    output = tmp_path / "out/function.zip"
    PACKAGER["write_archive"](source, environment, output)
    with zipfile.ZipFile(output) as zipped:
        assert zipped.read("virtualenv/bin/python") == b"native executable"
        assert "__main__.py" in zipped.namelist()
        assert "function.zip" not in zipped.namelist()


def test_job_needs_no_privilege_or_docker_socket():
    rendered = subprocess.check_output(["helm", "template", "flight-ticket", str(FLIGHT)], text=True)
    job = next(
        doc for doc in yaml.safe_load_all(rendered) if doc and doc.get("metadata", {}).get("name") == "deploy-actions"
    )
    spec = job["spec"]["template"]["spec"]
    assert not spec.get("volumes")
    assert not spec["containers"][0].get("volumeMounts")
    assert not spec["containers"][0].get("securityContext", {}).get("privileged", False)
    dockerfile = (DEPLOY / "Dockerfile").read_text()
    assert "FROM ${PYTHON_RUNTIME_IMAGE} AS packages" in dockerfile
    assert "COPY --from=packages" in dockerfile
    assert "docker.io" not in dockerfile
    assert "docker run" not in (DEPLOY / "deploy_ow_actions.sh").read_text()
