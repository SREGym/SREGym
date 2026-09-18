import pytest

from sregym import agent_launcher
from sregym.service.container_runner import HARDENING_FLAGS, ContainerConfig, ContainerRunner, ExecInput
from sregym.service.internet_policy import InternetPolicy


def make_runner(**kwargs):
    kwargs.setdefault("internet_policy", InternetPolicy.from_mode("open"))
    return ContainerRunner(ContainerConfig(**kwargs))


def test_hardening_flags_applied_by_default():
    args = make_runner()._build_base_docker_args()

    assert "--cap-drop=ALL" in args
    assert "--security-opt=no-new-privileges" in args


def test_dac_override_is_added_back():
    # /logs and /workspace are host-owned bind mounts; container root cannot
    # write through their mode bits without it.
    args = make_runner()._build_base_docker_args()

    assert "--cap-add=DAC_OVERRIDE" in args
    assert args.index("--cap-drop=ALL") < args.index("--cap-add=DAC_OVERRIDE")


def test_hardening_can_be_disabled():
    args = make_runner(harden_container=False)._build_base_docker_args()

    assert not any(flag in args for flag in HARDENING_FLAGS)


@pytest.mark.parametrize("mode", ["open", "filtered"])
def test_hardening_applies_under_every_internet_policy(tmp_path, mode):
    runner = make_runner(internet_policy=InternetPolicy.from_mode(mode))
    if runner.config.internet_policy.is_filtered:
        runner._egress_network_name = "private-network"
        runner._egress_proxy_name = "filter-proxy"
        runner._egress_proxy_ca = tmp_path / "ca.pem"
        runner._egress_ca_bundle = tmp_path / "bundle.pem"
        runner._egress_proxy_ca.touch()
        runner._egress_ca_bundle.touch()

    try:
        args = runner._build_base_docker_args()
    finally:
        runner.cleanup_credential_tmps()

    assert all(flag in args for flag in HARDENING_FLAGS)


def test_flags_precede_the_image_in_the_full_command():
    # Docker only honours run options before the image argument.
    runner = make_runner(image="test-image:latest")
    try:
        cmd = runner.build_docker_command(ExecInput(command="echo hi", label="t"))
    finally:
        runner.cleanup_credential_tmps()

    image_index = cmd.index("test-image:latest")
    for flag in HARDENING_FLAGS:
        assert cmd.index(flag) < image_index
    assert cmd[-1] == "echo hi"


@pytest.mark.parametrize(("enabled", "expected"), [(True, True), (False, False)])
def test_launcher_passes_hardening_choice_to_the_container_config(monkeypatch, enabled, expected):
    captured = {}

    class StubRunner:
        def __init__(self, config):
            captured["config"] = config

        def ensure_image_exists(self):
            pass

    monkeypatch.setattr(agent_launcher, "ContainerRunner", StubRunner)
    launcher = agent_launcher.AgentLauncher()
    launcher.set_container_hardening(enabled)
    launcher.enable_container_isolation()

    assert captured["config"].harden_container is expected


def test_launcher_hardens_by_default(monkeypatch):
    captured = {}

    class StubRunner:
        def __init__(self, config):
            captured["config"] = config

        def ensure_image_exists(self):
            pass

    monkeypatch.setattr(agent_launcher, "ContainerRunner", StubRunner)
    agent_launcher.AgentLauncher().enable_container_isolation()

    assert captured["config"].harden_container is True


def test_hardening_cannot_change_after_the_runner_exists(monkeypatch):
    class StubRunner:
        def __init__(self, config):
            pass

        def ensure_image_exists(self):
            pass

    monkeypatch.setattr(agent_launcher, "ContainerRunner", StubRunner)
    launcher = agent_launcher.AgentLauncher()
    launcher.enable_container_isolation()

    with pytest.raises(RuntimeError):
        launcher.set_container_hardening(False)
