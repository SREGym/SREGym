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
import yaml

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
        "environment/docker-compose.yaml",
        "environment/sregym-ready",
        "solution/solve.sh",
        "tests/Dockerfile",
        "tests/test.sh",
        "tests/score.py",
    }
    for executable in ("solution/solve.sh", "tests/test.sh", "environment/sregym-ready"):
        assert (task_dir / executable).stat().st_mode & 0o111

    config = tomllib.loads((task_dir / "task.toml").read_text())
    assert config["task"]["name"] == "sregym/wrong-service-selector-hotel-reservation"
    assert config["metadata"]["sregym_problem_id"] == "wrong_service_selector_hotel_reservation"
    assert config["verifier"]["environment_mode"] == "separate"
    [hook] = config["verifier"]["collect"]
    assert hook["service"] == protocol.SERVICE_NAME
    assert f"127.0.0.1:{protocol.GRADE_PORT}/grade" in hook["command"]
    assert {"source": protocol.GRADE_PATH, "service": protocol.SERVICE_NAME} in config["artifacts"]
    assert config["environment"]["healthcheck"]["command"].startswith("sregym-ready ")

    compose = yaml.safe_load((task_dir / "environment/docker-compose.yaml").read_text())
    backend = compose["services"][protocol.SERVICE_NAME]
    assert backend["privileged"] is True
    assert backend["image"] == "${SREGYM_HARBOR_IMAGE:-example.test/sregym:1}"
    assert backend["environment"][protocol.PROBLEM_ID_ENV] == "wrong_service_selector_hotel_reservation"
    # The agent never gets the backend's privileges or writable shared state.
    main = compose["services"]["main"]
    assert "privileged" not in main
    assert main["volumes"] == [f"sregym-shared:{protocol.AGENT_SHARED_DIR}:ro"]


def test_sidecar_defaults_suit_cloud_providers(task_dir):
    compose = yaml.safe_load((task_dir / "environment/docker-compose.yaml").read_text())
    environment = compose["services"][protocol.SERVICE_NAME]["environment"]
    # Overridable when the task runs; an empty value disables the mirror.
    assert environment["SREGYM_REGISTRY_MIRROR"] == f"${{SREGYM_REGISTRY_MIRROR-{adapter.DEFAULT_REGISTRY_MIRROR}}}"
    assert environment["SREGYM_KIND_NODE_IMAGE"] == "${SREGYM_KIND_NODE_IMAGE-}"
    # Setup diagnostics land in the collected log directory, and a failed
    # setup is reported through the shared state the healthcheck reads.
    assert environment["SREGYM_DIND_RESULTS"].startswith(protocol.LOG_DIR + "/")
    assert environment["SREGYM_FAILURE_STATE_DIR"] == protocol.BACKEND_SHARED_DIR
    assert int(environment["SREGYM_HOLD_ON_FAILURE_S"]) > 0


def test_mirror_and_node_image_can_be_set(tmp_path):
    task = SREGymAdapter(tmp_path, registry_mirror="", kind_node_image="example.test/node:1").generate_task(_info())
    environment = yaml.safe_load((task / "environment/docker-compose.yaml").read_text())["services"][
        protocol.SERVICE_NAME
    ]["environment"]
    assert environment["SREGYM_REGISTRY_MIRROR"] == "${SREGYM_REGISTRY_MIRROR-}"
    assert environment["SREGYM_KIND_NODE_IMAGE"] == "${SREGYM_KIND_NODE_IMAGE-example.test/node:1}"


def test_dind_setup_failures_use_the_backend_state_files(tmp_path):
    # The DinD entrypoint fails before the backend exists, so it writes the
    # backend's state files itself; sregym-ready must read them the same way.
    script = (ROOT / "docker/dind/entrypoint.sh").read_text()
    function = re.search(r"^report_setup_failure\(\) \{\n.*?^\}\n", script, re.S | re.M).group(0)
    shared = tmp_path / "shared"
    subprocess.run(
        ["bash", "-c", function + 'stage="KIND cluster"; results=/logs; report_setup_failure 3'],
        env={**os.environ, "SREGYM_FAILURE_STATE_DIR": str(shared), "SREGYM_HOLD_ON_FAILURE_S": "0"},
        check=True,
        capture_output=True,
    )
    assert (shared / protocol.STATE_NAME).read_text().strip() == protocol.STATE_FAILED
    status = json.loads((shared / protocol.STATUS_NAME).read_text())
    assert status["state"] == protocol.STATE_FAILED
    assert "KIND cluster" in status["error"]


def test_only_the_oracle_secret_unlocks_recovery(task_dir, tmp_path):
    secret = (tmp_path / protocol.ORACLE_SECRET_FILE).read_text().strip()
    compose = yaml.safe_load((task_dir / "environment/docker-compose.yaml").read_text())
    expected = compose["services"][protocol.SERVICE_NAME]["environment"][protocol.ORACLE_TOKEN_SHA256_ENV]
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
    # instruction.md and the agent image are what the agent sees; the Compose
    # file and task.toml stay on the Harbor host.
    for name in ("instruction.md", "environment/Dockerfile", "environment/sregym-ready"):
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


def test_inspection_rejects_unknown_problems():
    with pytest.raises(ValueError, match="not_a_problem"):
        adapter.inspect_problems(["not_a_problem"])
