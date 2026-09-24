"""Tests for the Harbor task generator. No cluster or Docker daemon required."""

import hashlib
import importlib.util
import json
import re
import tomllib

import pytest
import yaml

from sregym.harbor import adapter, protocol
from sregym.harbor.adapter import ProblemInfo, SREGymAdapter


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


def test_only_the_reference_solution_can_trigger_recovery(task_dir, tmp_path):
    token = re.search(r"Bearer (\S+)\"", (task_dir / "solution/solve.sh").read_text()).group(1)
    compose = yaml.safe_load((task_dir / "environment/docker-compose.yaml").read_text())
    expected = compose["services"][protocol.SERVICE_NAME]["environment"][protocol.ORACLE_TOKEN_SHA256_ENV]
    assert hashlib.sha256(token.encode()).hexdigest() == expected

    # Tokens differ per generated task.
    other = SREGymAdapter(tmp_path / "other").generate_task(_info())
    assert token not in (other / "solution/solve.sh").read_text()


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


def test_unrendered_placeholders_are_rejected():
    with pytest.raises(ValueError, match="placeholder"):
        adapter._render("value: {{missing}}", {})


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
    inspection = adapter.inspect_problems(["network_policy_block", "latent_sector_error"])
    assert [info.problem_id for info in inspection.eligible] == ["network_policy_block"]
    assert inspection.eligible[0].namespaces == ["hotel-reservation"]
    assert "Khaos" in inspection.skipped["latent_sector_error"]


def test_inspection_rejects_unknown_problems():
    with pytest.raises(ValueError, match="not_a_problem"):
        adapter.inspect_problems(["not_a_problem"])
