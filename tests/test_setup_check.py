import json
from pathlib import Path

import pytest

from sregym import doctor
from sregym.doctor import CheckResult, Doctor, DoctorOptions, Status

READY_NODE = {"metadata": {"name": "kind-worker"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
NOT_READY_NODE = {
    "metadata": {"name": "kind-worker2"},
    "status": {"conditions": [{"type": "Ready", "status": "False"}]},
}


class FakeRunner:
    """Answer commands by their first matching prefix; record every call."""

    def __init__(self, responses: dict[str, tuple[int, str]]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, argv, timeout=20):
        command = " ".join(argv)
        self.calls.append(command)
        for prefix, response in self.responses.items():
            if command.startswith(prefix):
                return response
        return 127, f"{argv[0]}: command not found"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[tool.uv]\nrequired-version = ">=0.11.8"\n')
    (tmp_path / "SREGym-applications").mkdir()
    (tmp_path / "SREGym-applications" / "README.md").write_text("apps")
    (tmp_path / "agents.yaml").write_text(
        "agents:\n"
        "  - name: stratus\n"
        "    kickoff_command: python -m clients.stratus\n"
        "  - name: codex\n"
        "    kickoff_command: python -m clients.codex.driver\n"
        "  - name: legacy\n"
        "    kickoff_command: python legacy.py\n"
        "    container_isolation: false\n"
    )
    return tmp_path


def make(repo, responses=None, env=None, **options):
    runner = FakeRunner(responses or {})
    opts = DoctorOptions(environment=env or {}, live_judge=False, **options)
    return Doctor(opts, runner=runner, repo_root=repo), runner


def cluster(nodes):
    return {"kubectl get nodes": (0, json.dumps({"items": nodes})), "kubectl config current-context": (0, "kind-kind")}


def test_old_uv_is_a_blocker(repo):
    check, _ = make(repo, {"uv --version": (0, "uv 0.11.3 (abc 2026-01-01)")})
    result = check.check_uv()
    assert result.status == Status.FAIL
    assert "0.11.3" in result.detail and result.fix == "uv self update"


def test_current_uv_passes(repo):
    check, _ = make(repo, {"uv --version": (0, "uv 0.13.7")})
    assert check.check_uv().status == Status.OK


def test_empty_applications_submodule_is_a_blocker(repo):
    (repo / "SREGym-applications" / "README.md").unlink()
    result = make(repo)[0].check_submodule()
    assert result.status == Status.FAIL
    assert "submodule update" in result.fix


def test_docker_down_skips_image_check(repo):
    check, _ = make(repo, {"docker info": (1, "Cannot connect to the Docker daemon")})
    [result] = check.check_docker()
    assert result.status == Status.FAIL
    assert check.check_agent_image().status == Status.SKIP


def test_small_docker_vm_explains_the_cap(repo, monkeypatch):
    monkeypatch.setattr(doctor, "_host_memory_gb", lambda: 15.0)
    info = {"ServerVersion": "28.5.1", "NCPU": 12, "MemTotal": 4 * 1024**3, "OperatingSystem": "Docker Desktop"}
    check, _ = make(repo, {"docker info": (0, json.dumps(info))})
    size = next(r for r in check.check_docker() if r.name == "Machine size")
    assert size.status == Status.WARN
    assert "this host has 15 GB" in size.detail
    assert "Docker Desktop settings" in size.fix
    assert "--profile svelte" in size.fix


def test_large_machine_passes_size_check(repo):
    info = {"NCPU": 16, "MemTotal": 32 * 1024**3, "OperatingSystem": "Ubuntu"}
    check, _ = make(repo, {"docker info": (0, json.dumps(info))})
    size = next(r for r in check.check_docker() if r.name == "Machine size")
    assert size.status == Status.OK


def test_ready_cluster(repo):
    check, _ = make(repo, cluster([READY_NODE]))
    result = check.check_cluster()
    assert result.status == Status.OK
    assert "1/1 nodes Ready" in result.detail


def test_not_ready_node_is_a_blocker(repo):
    check, _ = make(repo, cluster([READY_NODE, NOT_READY_NODE]))
    assert check.check_cluster().status == Status.FAIL


def test_deleted_kind_cluster_is_named(repo):
    responses = {
        "kubectl get nodes": (1, 'Get "https://127.0.0.1:39829/api": dial tcp: connect: connection refused'),
        "kubectl config current-context": (0, "kind-kind"),
        "kind get clusters": (0, "No kind clusters found."),
    }
    result = make(repo, responses)[0].check_cluster()
    assert result.status == Status.FAIL
    assert "no longer exists" in result.detail


def test_stopped_kind_nodes_get_a_start_command(repo):
    responses = {
        "kubectl get nodes": (1, 'Get "https://127.0.0.1:39829/api": dial tcp: connect: connection refused'),
        "kubectl config current-context": (0, "kind-kind"),
        "kind get clusters": (0, "kind\n"),
        "docker ps -a": (0, "kind-control-plane exited\nkind-worker exited\n"),
    }
    result = make(repo, responses)[0].check_cluster()
    assert "127.0.0.1:39829 refused the connection" in result.detail
    assert result.fix == "docker start kind-control-plane kind-worker"


def test_kubectl_noise_is_summarized():
    noise = (
        'E1106 14:21:38 memcache.go:265] "Unhandled Error" err="couldn\'t get current server API group list: '
        'Get \\"https://127.0.0.1:39829/api?timeout=32s\\": dial tcp 127.0.0.1:39829: connect: connection refused"'
    )
    assert doctor._summarize_kubectl_error(noise) == "https://127.0.0.1:39829 refused the connection"


def test_missing_udev_is_a_blocker_on_full_profile(repo):
    responses = {**cluster([READY_NODE]), "docker info": (0, "{}"), "docker exec kind-worker": (1, "")}
    check, _ = make(repo, responses)
    check.check_docker()
    check.check_cluster()
    result = check.check_udev()
    assert result.status == Status.FAIL
    assert "kind-worker" in result.detail


def test_udev_is_skipped_on_svelte(repo):
    check, runner = make(repo, profile="svelte")
    assert check.check_udev().status == Status.SKIP
    assert runner.calls == []


def test_missing_calico_tier_blocks_filtered_mode(repo):
    responses = {**cluster([READY_NODE]), "kubectl get tiers": (1, "NotFound")}
    check, _ = make(repo, responses)
    check.check_cluster()
    result = check.check_calico_tier()
    assert result.status == Status.FAIL
    assert "--internet-access open" in result.fix


def test_calico_tier_not_needed_in_open_mode(repo):
    check, runner = make(repo, internet_access="open")
    assert check.check_calico_tier().status == Status.SKIP
    assert runner.calls == []


def test_unknown_agent_lists_choices(repo):
    [result] = make(repo, agent="nope")[0].check_agent()
    assert result.status == Status.FAIL
    assert "codex" in result.fix and "stratus" in result.fix


def test_agent_without_container_cannot_use_filtered_mode(repo):
    results = make(repo, agent="legacy")[0].check_agent()
    allowlist = next(r for r in results if r.name == "Filtered network allowlist")
    assert allowlist.status == Status.FAIL


def test_unknown_provider_blocks_filtered_mode(repo):
    results = make(repo, agent="stratus", model="mystery-model")[0].check_agent()
    allowlist = next(r for r in results if r.name == "Filtered network allowlist")
    assert allowlist.status == Status.FAIL
    assert "AGENT_API_BASE" in allowlist.fix


def test_filtered_mode_reports_allowed_hosts(repo):
    env = {"ANTHROPIC_API_KEY": "x"}
    results = make(repo, env=env, agent="stratus", model="anthropic/claude-sonnet-4-6")[0].check_agent()
    allowlist = next(r for r in results if r.name == "Filtered network allowlist")
    assert allowlist.status == Status.OK
    assert "api.anthropic.com" in allowlist.detail


def test_missing_model_key_is_a_blocker(repo):
    results = make(repo, env={}, model="anthropic/claude-sonnet-4-6")[0].check_agent()
    creds = next(r for r in results if r.name == "Agent credentials")
    assert creds.status == Status.FAIL
    assert "ANTHROPIC_API_KEY" in creds.detail


def test_custom_base_without_key_is_a_blocker(repo):
    env = {"AGENT_API_BASE": "https://api.groq.com/openai/v1"}
    results = make(repo, env=env, model="openai/llama")[0].check_agent()
    creds = next(r for r in results if r.name == "Agent credentials")
    assert creds.status == Status.FAIL
    assert "AGENT_API_KEY" in creds.fix


def test_judge_inherits_agent_endpoint_like_a_run(repo):
    env = {"AGENT_API_BASE": "https://api.groq.com/openai/v1", "AGENT_API_KEY": "k"}
    assert make(repo, env=env, model="openai/llama")[0].check_judge().status == Status.OK


def test_separate_judge_model_does_not_inherit_agent_key(repo):
    env = {"AGENT_API_BASE": "https://api.groq.com/openai/v1", "AGENT_API_KEY": "k"}
    result = make(repo, env=env, model="openai/llama", judge_model="anthropic/claude-sonnet-4-6")[0].check_judge()
    assert result.status == Status.FAIL
    assert "ANTHROPIC_API_KEY" in result.detail


def test_subscription_judge_without_login_is_a_blocker(repo, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    result = make(repo, judge_backend="claudecode")[0].check_judge()
    assert result.status == Status.FAIL
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.detail


def test_judge_ping_failure_is_reported(repo, monkeypatch):
    class Broken:
        def inference(self, *args, **kwargs):
            raise RuntimeError("402 Payment Required: prepaid credits depleted")

    monkeypatch.setattr("llm_backend.init_backend.get_llm_backend_for_judge", lambda: Broken())
    check, _ = make(repo, env={"JUDGE_API_BASE": "https://x", "JUDGE_API_KEY": "k"})
    check.options.live_judge = True
    result = check.check_judge()
    assert result.status == Status.FAIL
    assert "402" in result.detail


def test_busy_port_is_a_warning(repo, monkeypatch):
    monkeypatch.setattr(doctor, "_port_in_use", lambda port: port == 8000)
    result = make(repo)[0].check_ports()
    assert result.status == Status.WARN
    assert "8000 (API_PORT)" in result.detail


def test_exit_code_reflects_blockers(monkeypatch):
    from rich.console import Console

    console = Console(record=True, width=120)
    monkeypatch.setattr(Doctor, "run_all", lambda self: [CheckResult("x", Status.WARN, "meh", "do y")])
    assert doctor.run_doctor(DoctorOptions(environment={}), console=console) == 0
    monkeypatch.setattr(Doctor, "run_all", lambda self: [CheckResult("x", Status.FAIL, "broken", "fix it")])
    assert doctor.run_doctor(DoctorOptions(environment={}), console=console) == 1
    assert "1 blocker(s)" in console.export_text()


def test_doctor_never_runs_mutating_commands(repo, monkeypatch):
    monkeypatch.setattr(doctor, "_port_in_use", lambda port: False)
    responses = {
        "uv --version": (0, "uv 0.13.7"),
        "docker info": (0, json.dumps({"NCPU": 16, "MemTotal": 32 * 1024**3})),
        "docker image inspect": (0, "[]"),
        "docker exec": (0, ""),
        "docker run": (0, "8000 open\n9954 open\n16443 open\n"),
        "kubectl get tiers": (0, "tier/adminnetworkpolicy"),
        **cluster([READY_NODE]),
    }
    env = {"ANTHROPIC_API_KEY": "x"}
    check, runner = make(repo, responses, env=env, model="anthropic/claude-sonnet-4-6")
    results = check.run_all()
    assert not [r for r in results if r.status == Status.FAIL], results
    for call in runner.calls:
        verb = call.split()[:2]
        assert verb[0] in {"uv", "docker", "kubectl", "kind"}
        assert not any(word in call.split() for word in ("apply", "delete", "create", "pull", "start", "patch"))
        if call.startswith("docker run"):  # the reachability probe: throwaway, never pulls
            assert "--rm" in call.split() and "--pull=never" in call.split()


def _all_good(repo, monkeypatch, **overrides):
    monkeypatch.setattr(doctor, "_port_in_use", lambda port: False)
    responses = {
        "docker info": (0, json.dumps({"NCPU": 16, "MemTotal": 32 * 1024**3})),
        "docker exec": (0, ""),
        "docker run": (0, "8000 open\n9954 open\n16443 open\n"),
        "kubectl get tiers": (0, "tier/adminnetworkpolicy"),
        **cluster([READY_NODE]),
        **overrides,
    }
    return responses


def test_pre_run_blocks_on_a_certain_failure(repo, monkeypatch):
    responses = _all_good(repo, monkeypatch, **{"kubectl get tiers": (1, "NotFound")})
    check, _ = make(repo, responses, env={"ANTHROPIC_API_KEY": "x"}, model="anthropic/claude-sonnet-4-6")
    check.options.pre_run = True
    tier = next(r for r in check.run_all() if r.name == "Calico adminnetworkpolicy tier")
    assert tier.status == Status.FAIL


def test_pre_run_only_warns_on_inferred_credentials(repo, monkeypatch):
    responses = _all_good(repo, monkeypatch)
    check, _ = make(repo, responses, env={}, model="anthropic/claude-sonnet-4-6")
    check.options.pre_run = True
    results = check.run_all()
    assert not [r for r in results if r.status == Status.FAIL], results
    creds = next(r for r in results if r.name == "Agent credentials")
    assert creds.status == Status.WARN


def test_pre_run_skips_uv_and_image_and_judge_request(repo, monkeypatch):
    def no_judge_requests():
        raise AssertionError("the run's own judge preflight sends the test request")

    monkeypatch.setattr("llm_backend.init_backend.get_llm_backend_for_judge", no_judge_requests)
    responses = _all_good(repo, monkeypatch)
    runner = FakeRunner(responses)
    options = DoctorOptions(environment={"ANTHROPIC_API_KEY": "x"}, model="anthropic/claude-sonnet-4-6")
    monkeypatch.setattr(doctor, "Doctor", lambda opts: Doctor(opts, runner=runner, repo_root=repo))
    from rich.console import Console

    console = Console(record=True, width=120)
    assert doctor.run_pre_run_doctor(options, console=console) is True
    assert console.export_text() == ""  # a run that can start prints nothing extra
    assert not [c for c in runner.calls if c.startswith(("uv ", "docker image"))]


def test_pre_run_stops_and_explains_how_to_skip(repo, monkeypatch):
    responses = _all_good(repo, monkeypatch, **{"kubectl get nodes": (1, "connection refused")})
    runner = FakeRunner(responses)
    monkeypatch.setattr(doctor, "Doctor", lambda opts: Doctor(opts, runner=runner, repo_root=repo))
    from rich.console import Console

    console = Console(record=True, width=120)
    assert doctor.run_pre_run_doctor(DoctorOptions(environment={}), console=console) is False
    text = console.export_text()
    assert "--skip-doctor" in text
    assert "Kubernetes cluster" in text
    assert "Applications submodule" not in text  # passing checks are not listed


def test_docker_is_not_required_without_agent_containers(repo, monkeypatch):
    responses = _all_good(repo, monkeypatch, **{"docker info": (1, "Cannot connect to the Docker daemon")})
    check, _ = make(repo, responses, env={"ANTHROPIC_API_KEY": "x"}, model="anthropic/claude-sonnet-4-6")
    check.options.pre_run = True
    check.options.use_external_harness = True
    docker_result = next(r for r in check.run_all() if r.name == "Docker")
    assert docker_result.status == Status.WARN


def test_custom_endpoint_is_ignored_for_agents_that_do_not_use_it(repo):
    env = {"AGENT_API_BASE": "https://api.groq.com/openai/v1", "AGENT_API_KEY": "k"}
    check, _ = make(repo, env=env, agent="codex", model="anthropic/claude-sonnet-4-6")
    check.options.agent = "claudecode"
    creds = check._check_agent_credentials()
    assert creds.status == Status.FAIL
    assert "ANTHROPIC_API_KEY" in creds.fix


def test_judge_names_the_endpoint_it_will_use(repo):
    env = {"JUDGE_API_BASE": "https://api.groq.com/openai/v1", "JUDGE_API_KEY": "k"}
    result = make(repo, env=env, judge_model="anthropic/claude-sonnet-4-6")[0].check_judge()
    assert result.status == Status.OK
    assert "via https://api.groq.com/openai/v1" in result.detail


@pytest.mark.parametrize(
    ("host_gb", "docker_gb", "expected"),
    [
        (32.0, 8.0, "to about 16 GB"),
        (15.3, 3.7, "to about 11 GB"),
        (15.3, 10.2, "as much memory as this host can spare"),
    ],
)
def test_memory_advice_fits_the_host(repo, monkeypatch, host_gb, docker_gb, expected):
    monkeypatch.setattr(doctor, "_host_memory_gb", lambda: host_gb)
    info = {"NCPU": 12, "MemTotal": int(docker_gb * 1024**3), "OperatingSystem": "Docker Desktop"}
    check, _ = make(repo, {"docker info": (0, json.dumps(info))}, profile="svelte")
    size = next(r for r in check.check_docker() if r.name == "Machine size")
    assert expected in size.fix


def test_kubectl_json_before_the_error_is_skipped():
    output = '{\n    "apiVersion": "v1",\n    "items": [],\n}\nThe connection to the server 127.0.0.1:37659 was refused - did you specify the right host or port?'
    assert doctor._summarize_kubectl_error(output) == "127.0.0.1:37659 refused the connection"
    assert (
        doctor._summarize_kubectl_error('{\n"items": []\n}\nerror: You must be logged in')
        == "error: You must be logged in"
    )


def test_stopped_node_is_named_with_a_start_command_and_no_udev_false_alarm(repo):
    responses = {
        **cluster([READY_NODE, NOT_READY_NODE]),
        "docker info": (0, "{}"),
        "docker ps -a --filter status=exited": (0, "kind-worker2\n"),
        "docker exec kind-worker ": (0, ""),
        "docker exec kind-worker2": (1, "Error response from daemon: container abc is not running"),
    }
    check, _ = make(repo, responses)
    check.check_docker()
    result = check.check_cluster()
    assert result.status == Status.FAIL
    assert "not Ready: kind-worker2" in result.detail
    assert result.fix == "docker start kind-worker2"
    assert check.check_udev().status == Status.OK


def _docker_up():
    return {"docker info": (0, json.dumps({"NCPU": 16, "MemTotal": 32 * 1024**3}))}


def test_firewalled_host_ports_block_with_a_ufw_fix(repo):
    responses = {
        **_docker_up(),
        "docker run": (0, "8000 blocked\n9954 blocked\n16443 open\n"),
        "systemctl is-active ufw": (0, "active\n"),
    }
    check, _ = make(repo, responses)
    check.check_docker()
    result = check.check_host_reachable()
    assert result.status == Status.FAIL
    assert "8000, 9954" in result.detail and "16443" not in result.detail
    assert "sudo ufw allow from 172.16.0.0/12 to any port 8000,9954 proto tcp" in result.fix


def test_firewall_fix_without_ufw_is_generic(repo):
    responses = {**_docker_up(), "docker run": (0, "8000 blocked\n9954 open\n16443 open\n")}
    check, _ = make(repo, responses)
    check.check_docker()
    result = check.check_host_reachable()
    assert result.status == Status.FAIL
    assert "ufw" not in result.fix and "172.16.0.0/12" in result.fix


def test_reachable_host_ports_pass_and_follow_env_overrides(repo):
    responses = {**_docker_up(), "docker run": (0, "8100 open\n9954 open\n16443 open\n")}
    check, runner = make(repo, responses, env={"API_PORT": "8100"})
    check.check_docker()
    assert check.check_host_reachable().status == Status.OK
    [probe] = [c for c in runner.calls if c.startswith("docker run")]
    assert probe.endswith(" 8100 9954 16443")


def test_reachability_is_skipped_when_image_is_not_pulled(repo):
    responses = {**_docker_up(), "docker run": (125, "docker: Error response from daemon: No such image: x")}
    check, _ = make(repo, responses)
    check.check_docker()
    assert check.check_host_reachable().status == Status.SKIP


def test_reachability_is_skipped_without_agent_containers(repo):
    check, runner = make(repo, _docker_up(), use_external_harness=True)
    check.check_docker()
    assert check.check_host_reachable().status == Status.SKIP
    assert not [c for c in runner.calls if c.startswith("docker run")]


def test_firewalled_host_ports_block_a_run(repo, monkeypatch):
    responses = _all_good(repo, monkeypatch, **{"docker run": (0, "8000 blocked\n9954 open\n16443 open\n")})
    check, _ = make(repo, responses, env={"ANTHROPIC_API_KEY": "x"}, model="anthropic/claude-sonnet-4-6")
    check.options.pre_run = True
    reach = next(r for r in check.run_all() if r.name == "Container to host")
    assert reach.status == Status.FAIL


def test_pre_run_is_silent_on_warnings(repo, monkeypatch):
    runner = FakeRunner(_all_good(repo, monkeypatch))
    monkeypatch.setattr(doctor, "_port_in_use", lambda port: True)  # a warning, not a blocker
    monkeypatch.setattr(doctor, "Doctor", lambda opts: Doctor(opts, runner=runner, repo_root=repo))
    from rich.console import Console

    console = Console(record=True, width=120)
    options = DoctorOptions(environment={"ANTHROPIC_API_KEY": "x"}, model="anthropic/claude-sonnet-4-6")
    assert doctor.run_pre_run_doctor(options, console=console) is True
    assert console.export_text() == ""
