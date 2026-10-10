"""Tests for the Harbor task generator. No cluster or Docker daemon required."""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from sregym.harbor import adapter, protocol
from sregym.harbor.adapter import ProblemInfo, SREGymAdapter

ROOT = Path(__file__).resolve().parents[2]


def _info(**overrides) -> ProblemInfo:
    values = {
        "problem_id": "wrong_service_selector_hotel_reservation",
        "app_name": "Hotel Reservation",
        "app_description": "A hotel reservation application.",
        "namespaces": ["hotel-reservation"],
    }
    return ProblemInfo(**(values | overrides))


@pytest.fixture
def task_dir(tmp_path):
    return SREGymAdapter(tmp_path, backend_image="example.test/sregym:1").generate_task(_info())


def test_task_names_are_harbor_safe():
    assert adapter.task_name("k8s_target_port-misconfig") == "k8s-target-port-misconfig"
    assert adapter.task_name("Network_Policy_Block") == "network-policy-block"


def test_generated_task_is_complete_and_parses(task_dir):
    files = {path.relative_to(task_dir).as_posix() for path in task_dir.rglob("*") if path.is_file()}
    assert files == {
        "README.md",
        "instruction.md",
        "task.toml",
        "environment/Dockerfile",
        "solution/solve.sh",
        "tests/test.sh",
        "tests/score.py",
    }
    for executable in ("solution/solve.sh", "tests/test.sh"):
        assert (task_dir / executable).stat().st_mode & 0o111

    config = tomllib.loads((task_dir / "task.toml").read_text())
    assert config["task"]["name"] == "sregym/wrong-service-selector-hotel-reservation"
    assert config["metadata"]["sregym_problem_id"] == "wrong_service_selector_hotel_reservation"
    assert config["environment"]["healthcheck"]["command"].startswith("sregym-ready ")
    assert config["artifacts"] == [{"source": protocol.LOG_DIR}]
    # One container: the verifier grades in it as root, the agent runs unprivileged.
    assert config["verifier"]["environment_mode"] == "shared"
    assert "user" not in config["verifier"]
    assert config["agent"]["user"] == "agent"

    test_sh = (task_dir / "tests/test.sh").read_text()
    assert "pkill -KILL -u agent" in test_sh
    assert f"127.0.0.1:{protocol.GRADE_PORT}/grade" in test_sh
    assert protocol.GRADE_TOKEN_PATH in test_sh


def test_task_image_keeps_the_problem_root_only(task_dir):
    dockerfile = (task_dir / "environment/Dockerfile").read_text()
    assert dockerfile.splitlines()[3] == "FROM example.test/sregym:1"
    assert "echo 'wrong_service_selector_hotel_reservation' > /etc/sregym/problem" in dockerfile
    assert "install -d -m 700 /etc/sregym" in dockerfile
    assert "chmod 600 /etc/sregym/problem" in dockerfile
    # Nothing in the files that configure the container asks for privileges.
    for name in ("task.toml", "environment/Dockerfile"):
        assert "privileged" not in (task_dir / name).read_text().replace("unprivileged", ""), name


def test_setup_failures_use_the_backend_state_files(tmp_path):
    # start.sh fails before the backend exists, so it writes the backend's
    # state files itself; sregym-ready must read them the same way.
    script = (ROOT / "docker/harbor/start.sh").read_text()
    function = re.search(r"^fail\(\) \{\n.*?^\}\n", script, re.S | re.M).group(0)
    shared = tmp_path / "shared"
    shared.mkdir()
    subprocess.run(
        ["bash", "-c", f"shared={shared}; out=/out; " + function + 'stage="cluster"; fail'],
        check=True,
        capture_output=True,
    )
    assert (shared / protocol.STATE_NAME).read_text().strip() == protocol.STATE_FAILED
    status = json.loads((shared / protocol.STATUS_NAME).read_text())
    assert status["state"] == protocol.STATE_FAILED
    assert "cluster" in status["error"]
    ready = (ROOT / "docker/harbor/sregym-ready").read_text()
    assert f"$shared/{protocol.STATE_NAME}" in ready and f"$shared/{protocol.STATUS_NAME}" in ready


def test_only_the_oracle_secret_unlocks_recovery(task_dir, tmp_path):
    secret = (tmp_path / protocol.ORACLE_SECRET_FILE).read_text().strip()
    dockerfile = (task_dir / "environment/Dockerfile").read_text()
    expected = re.search(r"echo '([0-9a-f]{64})' > /etc/sregym/oracle-token-sha256", dockerfile).group(1)
    token = adapter.oracle_token(secret, task_dir.name)
    assert hashlib.sha256(token.encode()).hexdigest() == expected

    # The reference solution derives the token at run time from the secret,
    # which Harbor passes to the oracle agent only.
    config = tomllib.loads((task_dir / "task.toml").read_text())
    assert config["solution"]["env"] == {protocol.ORACLE_SECRET_ENV: f"${{{protocol.ORACLE_SECRET_ENV}}}"}
    solve = task_dir / "solution/solve.sh"
    derived = subprocess.run(
        ["bash", "-c", solve.read_text().split("curl ")[0] + 'printf %s "$token"'],
        env={**os.environ, protocol.ORACLE_SECRET_ENV: secret},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert derived == token

    # Nothing that gets published holds the secret or a usable token.
    for path in task_dir.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text() and token not in path.read_text(), path
    assert (tmp_path / protocol.ORACLE_SECRET_FILE).stat().st_mode & 0o077 == 0


def test_oracle_secret_comes_from_the_environment_or_the_dataset_root(tmp_path, monkeypatch):
    monkeypatch.delenv(protocol.ORACLE_SECRET_ENV, raising=False)
    first = SREGymAdapter(tmp_path / "a")
    first.generate_task(_info())
    # Later runs into the same dataset reuse its secret; other datasets differ.
    again = SREGymAdapter(tmp_path / "a", overwrite=True)
    again.generate_task(_info())
    assert again.oracle_secret == first.oracle_secret
    assert SREGymAdapter(tmp_path / "b").oracle_secret != first.oracle_secret

    monkeypatch.setenv(protocol.ORACLE_SECRET_ENV, "maintainer-secret")
    pinned = SREGymAdapter(tmp_path / "c")
    pinned.generate_task(_info())
    assert pinned.oracle_secret == "maintainer-secret"
    assert not (tmp_path / "c" / protocol.ORACLE_SECRET_FILE).exists()


def test_agent_visible_files_do_not_reveal_the_problem(task_dir):
    # instruction.md is what the agent sees; the task directory stays on the
    # Harbor host, and the image keeps the problem in a root-only file.
    for name in ("instruction.md",):
        text = (task_dir / name).read_text().lower()
        assert "wrong_service_selector" not in text
        assert "selector" not in text


def test_instruction_lists_every_namespace(tmp_path):
    info = _info(namespaces=["ns-a", "ns-b"])
    instruction = (SREGymAdapter(tmp_path).generate_task(info) / "instruction.md").read_text()
    assert "Namespaces: ns-a, ns-b" in instruction


def test_existing_tasks_require_overwrite(tmp_path):
    generator = SREGymAdapter(tmp_path)
    generator.generate_task(_info())
    with pytest.raises(FileExistsError):
        generator.generate_task(_info())
    SREGymAdapter(tmp_path, overwrite=True).generate_task(_info())


def test_dataset_readme_lists_every_task_for_harbor_hub(tmp_path):
    generator = SREGymAdapter(tmp_path, dataset_name="sregym/sregym-lite")
    generator.generate_task(_info())
    generator.generate_task(_info(problem_id="network_policy_block"))
    readme = generator.write_dataset_readme().read_text()
    assert readme.startswith("# SREGym-Lite\n")
    assert "harbor run -d sregym/sregym-lite " in readme
    assert "| `sregym/network-policy-block` | Hotel Reservation |" in readme
    assert "| `sregym/wrong-service-selector-hotel-reservation` | Hotel Reservation |" in readme
    assert "2 problems" in readme
    assert "@article{sregym:26" in readme


def test_unrendered_placeholders_are_rejected():
    with pytest.raises(ValueError, match="placeholder"):
        adapter._render("value: {{missing}}", {})
    # BibTeX's double braces are not placeholders.
    assert adapter._render("title = {{SREGym: A Benchmark}}", {}) == "title = {{SREGym: A Benchmark}}"


def _load_score(task_dir, grade_path, reward_path):
    spec = importlib.util.spec_from_file_location("score", task_dir / "tests/score.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.GRADE = grade_path
    module.REWARD = reward_path
    return module


@pytest.mark.parametrize(("success", "reward"), [(True, 1.0), (False, 0.0)])
def test_score_maps_the_mitigation_verdict(task_dir, tmp_path, success, reward):
    grade, reward_file = tmp_path / "grade.json", tmp_path / "reward.json"
    grade.write_text(json.dumps({"success": success, "mitigation": {"success": success}}))
    assert _load_score(task_dir, grade, reward_file).main() == 0
    assert json.loads(reward_file.read_text()) == {"reward": reward}


def test_score_reports_backend_failures_as_errors(task_dir, tmp_path):
    grade, reward_file = tmp_path / "grade.json", tmp_path / "reward.json"
    score = _load_score(task_dir, grade, reward_file)
    assert score.main() == 1
    grade.write_text(json.dumps({"success": False, "error": "backend is failed"}))
    assert score.main() == 1
    assert not reward_file.exists()


def test_inspection_skips_problems_that_cannot_run_on_kind():
    inspection = adapter.inspect_problems(["network_policy_block", "node_clock_drift_hotel_reservation"])
    assert [info.problem_id for info in inspection.eligible] == ["network_policy_block"]
    assert inspection.eligible[0].namespaces == ["hotel-reservation"]
    assert "non-emulated" in inspection.skipped["node_clock_drift_hotel_reservation"]


def test_inspection_skips_problems_the_unprivileged_cluster_cannot_run():
    # inspect_problems() rejects unknown IDs, so a renamed problem fails here.
    inspection = adapter.inspect_problems(["network_policy_block", *adapter.K3S_UNSUPPORTED])
    assert [info.problem_id for info in inspection.eligible] == ["network_policy_block"]
    assert inspection.skipped == adapter.K3S_UNSUPPORTED


def test_alert_graded_problems_run_steady_before_the_fault(tmp_path):
    inspection = adapter.inspect_problems(["network_policy_block", "astronomy_shop_ad_service_failure"])
    steady = {info.problem_id: info.steady_state_s for info in inspection.eligible}
    assert steady == {"network_policy_block": 0, "astronomy_shop_ad_service_failure": adapter.ALERT_STEADY_STATE_S}
    task = SREGymAdapter(tmp_path, backend_image="example.test/sregym:1").generate_task(
        _info(steady_state_s=adapter.ALERT_STEADY_STATE_S)
    )
    dockerfile = (task / "environment/Dockerfile").read_text()
    assert f"echo '{adapter.ALERT_STEADY_STATE_S}' > /etc/sregym/steady-state-seconds" in dockerfile
    assert "/etc/sregym/steady-state-seconds" in dockerfile.split("chmod 600", 1)[1]


def test_inspection_rejects_unknown_problems():
    with pytest.raises(ValueError, match="not_a_problem"):
        adapter.inspect_problems(["not_a_problem"])
