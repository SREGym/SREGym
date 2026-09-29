import pytest

from sregym.service.container_runner import ContainerConfig, ContainerRunner, ExecInput
from sregym.service.internet_policy import InternetPolicy


def make_runner(**env_vars):
    """A runner with no filtered egress, so building args never invokes Docker."""
    return ContainerRunner(ContainerConfig(env_vars=env_vars, internet_policy=InternetPolicy.from_mode("open")))


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A home directory holding ~/.aws, with no AWS state leaking from the host."""
    (tmp_path / ".aws").mkdir()
    monkeypatch.setattr("sregym.service.container_runner.Path.home", lambda: tmp_path)
    for var in (*ContainerRunner.AWS_CREDENTIAL_VARS, *ContainerRunner.MODEL_ID_VARS):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def aws_mounts(args):
    return [args[i + 1] for i, item in enumerate(args) if item == "-v" and "/root/.aws" in args[i + 1]]


def test_aws_dir_not_mounted_without_aws_credentials(fake_home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    runner = make_runner(AGENT_MODEL_ID="gpt-5")

    assert aws_mounts(runner._build_base_docker_args()) == []


def test_region_alone_does_not_mount_aws_dir(fake_home, monkeypatch):
    # Region says where to call, not which identity to call with.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    runner = make_runner(AGENT_MODEL_ID="gpt-5")

    assert aws_mounts(runner._build_base_docker_args()) == []


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("AWS_PROFILE", "bedrock"),
        ("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE"),
        ("AWS_ROLE_ARN", "arn:aws:iam::111122223333:role/bedrock"),
        ("AWS_BEARER_TOKEN_BEDROCK", "token"),
    ],
)
def test_aws_dir_mounted_when_credentials_selected(fake_home, monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    runner = make_runner()

    assert aws_mounts(runner._build_base_docker_args()) == [f"{fake_home / '.aws'}:/root/.aws:ro"]


@pytest.mark.parametrize(
    "model_id",
    [
        "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "bedrock/converse/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "bedrock-claude-sonnet-4.5",
        "amazon-bedrock/anthropic.claude-v2",
        "sagemaker/jumpstart-model",
    ],
)
def test_aws_model_id_mounts_aws_dir_without_any_aws_var(fake_home, model_id):
    # A default profile in ~/.aws/config sets no AWS_* var, so the model id has
    # to keep the mount alive on its own.
    runner = make_runner(AGENT_MODEL_ID=model_id)

    assert aws_mounts(runner._build_base_docker_args()) == [f"{fake_home / '.aws'}:/root/.aws:ro"]


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-5",
        "anthropic/claude-sonnet-4-6-20250627",
        "local/qwen3",
        # Provider names that merely start with an AWS one.
        "bedrockery/local-model",
        "sagemakerless/mock",
    ],
)
def test_non_aws_model_id_does_not_mount_aws_dir(fake_home, model_id):
    runner = make_runner(AGENT_MODEL_ID=model_id)

    assert aws_mounts(runner._build_base_docker_args()) == []


def test_empty_kickoff_value_masks_the_host_var(fake_home, monkeypatch):
    # _build_env_flags lets ExecInput.env overwrite with "", so the container
    # never sees the host value; the gate must not mount on it either.
    monkeypatch.setenv("AWS_PROFILE", "production")
    runner = make_runner()
    exec_input = ExecInput(command="true", env={"AWS_PROFILE": ""})

    try:
        cmd = runner.build_docker_command(exec_input)
    finally:
        runner.cleanup_credential_tmps()

    assert aws_mounts(cmd) == []


def test_bedrock_judge_does_not_mount_into_agent_container(fake_home):
    # The judge runs host-side in the conductor, not in the agent container.
    runner = make_runner(
        AGENT_MODEL_ID="gpt-5",
        JUDGE_MODEL_ID="bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    )

    assert aws_mounts(runner._build_base_docker_args()) == []


def test_kickoff_env_reaches_the_gate(fake_home):
    # kickoff_env arrives via ExecInput, not config.env_vars or os.environ.
    runner = make_runner()
    exec_input = ExecInput(command="true", env={"AWS_PROFILE": "bedrock"})

    try:
        cmd = runner.build_docker_command(exec_input)
    finally:
        runner.cleanup_credential_tmps()

    assert aws_mounts(cmd) == [f"{fake_home / '.aws'}:/root/.aws:ro"]


def test_missing_aws_dir_mounts_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("sregym.service.container_runner.Path.home", lambda: tmp_path)
    monkeypatch.setenv("AWS_PROFILE", "bedrock")
    runner = make_runner()

    assert aws_mounts(runner._build_base_docker_args()) == []


def claude_mounts(args):
    return [args[i + 1] for i, item in enumerate(args) if item == "-v" and "/root/.claude" in args[i + 1]]


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """A home holding Claude Code OAuth credentials, isolated from the host."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr("sregym.service.container_runner.Path.home", lambda: tmp_path)
    directory = tmp_path / ".claude"
    directory.mkdir()
    return directory


def write_oauth(directory, access_token="oauth-access-token"):
    import json

    path = directory / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": access_token}}))
    return path


def test_claude_oauth_credentials_are_copied_read_only_for_agents(claude_home):
    """A subscription-authenticated host must not start an unauthenticated agent."""
    write_oauth(claude_home)

    mounts = claude_mounts(make_runner()._build_base_docker_args())

    assert len(mounts) == 1
    source, destination, mode = mounts[0].rsplit(":", 2)
    assert (destination, mode) == ("/root/.claude/.credentials.json", "ro")
    # A copy, so a token refresh inside the container cannot rewrite host state.
    assert source != str(claude_home / ".credentials.json")


def test_shared_claude_auth_mounts_the_host_file_for_refreshes(claude_home):
    path = write_oauth(claude_home)
    config = ContainerConfig(internet_policy=InternetPolicy.from_mode("open"), claude_auth="shared")

    assert claude_mounts(ContainerRunner(config)._build_base_docker_args()) == [
        f"{path.resolve()}:/root/.claude/.credentials.json:rw"
    ]


def test_claude_auth_can_be_disabled(claude_home):
    write_oauth(claude_home)
    config = ContainerConfig(internet_policy=InternetPolicy.from_mode("open"), claude_auth="none")

    assert claude_mounts(ContainerRunner(config)._build_base_docker_args()) == []


def test_no_claude_mount_without_usable_oauth_credentials(claude_home):
    assert claude_mounts(make_runner()._build_base_docker_args()) == []

    # An API-key-only or malformed file is not subscription auth.
    (claude_home / ".credentials.json").write_text('{"other": true}')
    assert claude_mounts(make_runner()._build_base_docker_args()) == []

    (claude_home / ".credentials.json").write_text("not json")
    assert claude_mounts(make_runner()._build_base_docker_args()) == []


def test_symlinked_claude_credentials_are_refused(claude_home, tmp_path):
    real = write_oauth(tmp_path)
    (claude_home / ".credentials.json").symlink_to(real)

    assert claude_mounts(make_runner()._build_base_docker_args()) == []


def test_claude_credentials_are_not_handed_to_another_agent(claude_home):
    """A Codex attempt has no use for a Claude Code subscription token."""
    write_oauth(claude_home)
    config = ContainerConfig(internet_policy=InternetPolicy.from_mode("open", agent_name="codex"))

    assert claude_mounts(ContainerRunner(config)._build_base_docker_args()) == []


def test_claude_credentials_are_mounted_for_a_claude_code_attempt(claude_home):
    write_oauth(claude_home)
    config = ContainerConfig(internet_policy=InternetPolicy.from_mode("open", agent_name="claudecode"))

    assert len(claude_mounts(ContainerRunner(config)._build_base_docker_args())) == 1
