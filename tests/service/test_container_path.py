from sregym.service.container_runner import CONTAINER_PATH


def test_container_path_keeps_the_image_venv_ahead_of_system_python():
    """The agent image installs its Python packages in /opt/venv (#1127). The PATH set when the
    agent-tools volume is mounted must keep that venv ahead of the system interpreter."""
    parts = CONTAINER_PATH.split(":")
    assert "/opt/venv/bin" in parts
    assert parts.index("/opt/venv/bin") < parts.index("/usr/bin")
