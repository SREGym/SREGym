import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from time import sleep

# we added the ssh key to the ssh agent such that all of all the keys are carried with the ssh connection.
base = Path(__file__).resolve().parent

ENV = {
    **os.environ,
    "CI": "1",
    "NONINTERACTIVE": "1",
    "DEBIAN_FRONTEND": "noninteractive",
    "SUDO_ASKPASS": "/bin/false",
}
TIMEOUT = 1800


scripts = [
    "brew.sh",
    "go.sh",
    "docker.sh",
    "kind.sh",
]


def init_user_paths(user: str):
    """Initialize all global paths and user-dependent commands after username is known."""
    global SREGYM_DIR, SREGYM_ROOT, KIND_DIR, REMOTE_ENV, LOCAL_ENV, REMOTE_SELF_PATH
    SREGYM_DIR = Path(f"/users/{user}/SREGym").resolve()
    SREGYM_ROOT = SREGYM_DIR
    KIND_DIR = SREGYM_ROOT / "kind"
    REMOTE_ENV = f"/users/{user}/SREGym/.env"
    LOCAL_ENV = Path(__file__).resolve().parent.parent.parent / ".env"
    REMOTE_SELF_PATH = f"/users/{user}/e2e-testing-scripts/automating_tests.py"


def _read_nodes(path: str = "nodes.txt") -> list[str]:
    full_path = (base / path).resolve()
    if not full_path.exists():
        raise FileNotFoundError(f"nodes.txt not found at {full_path}")
    with open(full_path) as f:
        return [ln.strip() for ln in f if ln.strip()]


def _run(cmd: list[str]):
    print("$", " ".join(shlex.quote(x) for x in cmd))
    subprocess.run(cmd)


def scp_scripts_to_all(user, nodes_file: str = "nodes.txt"):
    """scp -r LOCAL_COPY_SRC -> ~/e2e-testing-scripts on each node."""

    if not Path(base).exists():
        raise FileNotFoundError(f"LOCAL_COPY_SRC not found: {base}")
    for host in _read_nodes(nodes_file):
        _run(["scp", "-r", "-o", "StrictHostKeyChecking=no", str(base), f"{host}:~"])


def run_installations_all(user, nodes_file: str = "nodes.txt"):
    """SSH each node and run this file with --installations in a tmux session named 'installations'."""
    tmux_cmd = (
        f"if tmux has-session -t installations; then tmux kill-session -t installations; fi; "
        f"tmux new-session -d -s installations "
        f"'bash -ic \"python3 {REMOTE_SELF_PATH} --installations; sleep infinity\"'"
    )
    for host in _read_nodes(nodes_file):
        _run(["ssh", host, tmux_cmd])


def run_setup_env_all(user, nodes_file: str = "nodes.txt"):
    """SSH each node and run this file with --setup-env in a detached tmux session."""
    for host in _read_nodes(nodes_file):
        print(f"\n=== [SSH setup-env] {host} ===")

        remote_tmux = (
            "tmux kill-session -t setup_env 2>/dev/null || true; "
            "tmux new-session -d -s setup_env "
            "'bash -ic \""
            "cd ~/e2e-testing-scripts && "
            "python3 automating_tests.py --setup-env 2>&1 | tee -a ~/setup_env_log.txt; "
            "sleep infinity\"'"
        )

        _run(["ssh", host, remote_tmux])
        print(f"Started tmux session 'setup_env' on {host} (log: ~/setup_env_log.txt)")


def run_shell_script(path: Path):
    """Run a shell script with Bash: ensure exec bit, then 'bash <script>'."""
    print(f"\n==> RUN: {path}")
    if not path.exists():
        print(f"Script {path.name} not found at {path}")
        return

    try:
        cmd = f"chmod +x {shlex.quote(str(path))}; bash {shlex.quote(str(path))}"
        subprocess.run(
            ["bash", "-c", cmd],
            env=ENV,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=True,
        )
        print(f"Executed {path.name} successfully.")
    except subprocess.TimeoutExpired:
        print(f"Timed out executing {path}")
    except subprocess.CalledProcessError as e:
        print(f"Error executing {path}: exit {e.returncode}")


def installations():
    SCRIPTS_DIR = Path.home() / "e2e-testing-scripts"
    for script in scripts:
        path = SCRIPTS_DIR / script
        if path.exists():
            run_shell_script(path)
        else:
            print(f"Script {script} not found at {path}")
            return
    install_python()
    install_git()


def _brew_exists(node: str) -> bool:
    """Check if Homebrew is installed on a remote node via SSH."""
    try:
        subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                node,
                "bash -ic 'command -v brew >/dev/null 2>&1'",
            ],
            env=ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
        )
        return True
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        return False


def read_file(file_path: Path) -> list[str]:
    with open(file_path) as f:
        res = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    return res


def comment_out_problems():
    nodes = _read_nodes("nodes.txt")
    problems = read_file("registry.txt")
    mapping = {}
    m = len(problems)
    n = len(nodes)
    for i, node in enumerate(nodes):
        start = i * m // n
        end = (i + 1) * m // n
        mapping[node] = problems[start:end]
    # comment out agent run too
    agent_run_lines = ["reg = get_agent(agent_name)", "if reg:", "await LAUNCHER.ensure_started(reg)"]
    for node, probs in mapping.items():
        for prob in problems:
            if prob not in mapping[node]:
                print(f"On node {node}, comment out line: {prob.strip()}")
                cmd = f'ssh -o StrictHostKeyChecking=no {node} "sed -i \'/\\"{prob}\\":/s/^/#/\' ~/SREGym/sregym/conductor/problems/registry.py"'
                subprocess.run(cmd, shell=True, check=True)
        for l in agent_run_lines:
            cmd = f"ssh -o StrictHostKeyChecking=no {node} \"sed -i '/{l}/ s/^/#/' ~/SREGym/main.py\""
            subprocess.run(cmd, shell=True, check=True)


def run_submit(user, nodes_file: str = "nodes.txt"):
    TMUX_CMD = (
        "tmux kill-session -t submission 2>/dev/null || true; "
        f"tmux new-session -d -s submission -c /users/{user}/e2e-testing-scripts"
        "'python3 auto_submit.py 2>&1 | tee -a ~/submission_log.txt; sleep infinity'"
    )
    # TMUX_CMD2 = "tmux new-session -d -s main_tmux 'echo $PATH; sleep infinity;'"
    TMUX_CMD2 = (
        "tmux new-session -d -s main_tmux "
        "'env -i PATH=/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:/usr/local/bin:/usr/bin:/bin "
        "HOME=$HOME TERM=$TERM "
        'bash -ic "echo PATH=\\$PATH; '
        "command -v kubectl; kubectl version --client || true; "
        "command -v helm || true; "
        f"cd /users/{user}/SREGym && "
        "~/SREGym/.venv/bin/python3 main.py --agent autosubmit 2>&1 | tee -a global_benchmark_log_$(date +%Y-%m-%d).txt; "
        "sleep infinity\"'"
    )

    with open(nodes_file) as f:
        nodes = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    for host in nodes:
        print(f"=== {host} ===")
        cmd = [
            "ssh",
            host,
            f"{TMUX_CMD}",
        ]
        cmd2 = [
            "ssh",
            host,
            f"{TMUX_CMD2}",
        ]
        try:
            subprocess.run(cmd2, check=True)
            print(f"Main script started successfully on {host}.")
            sleep(20)
            subprocess.run(cmd, check=True)
            print(f"Submission script started successfully on {host}.")

        except subprocess.CalledProcessError as e:
            print(f"Setup failed with return code {e.returncode}")


def install_git():
    try:
        _install_brew_if_needed()
        shellenv = _brew_shellenv_cmd()
        subprocess.run(
            ["bash", "-ic", f"{shellenv}; brew --version; brew install git"],
            env=ENV,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=True,
        )
        print("Git installed successfully.")
    except subprocess.TimeoutExpired:
        print("Timed out installing Git.")
    except subprocess.CalledProcessError as e:
        print(f"Error installing Git: exit {e.returncode}")


def clone(nodes_file: str = "nodes.txt", repo: str = "https://github.com/SREGym/SREGym"):
    """
    Clone the repo on all remote nodes using local SSH agent forwarding.
    """
    env = os.environ.copy()
    if "SSH_AUTH_SOCK" not in env or not env["SSH_AUTH_SOCK"]:
        raise EnvironmentError("No SSH agent detected. Run `ssh-add -l` to confirm your key is loaded.")

    remote_cmd = f'GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" git clone --recurse-submodules {repo} && cd SREGym && git checkout lily-e2e-test'

    with open(nodes_file) as f:
        nodes = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    for host in nodes:
        print(f"=== {host} ===")
        cmd = [
            "ssh",
            "-A",
            "-o",
            "StrictHostKeyChecking=no",
            host,
            remote_cmd,
        ]
        try:
            subprocess.run(cmd, check=True, env=env)
            subprocess.run(
                [
                    "scp",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    str(LOCAL_ENV),
                    f"{host}:~/SREGym/.env",
                ],
                check=True,
                env=env,
            )
            subprocess.run(
                [
                    "ssh",
                    "-A",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    host,
                    "sed -i '/^API_KEY.*/d' ~/SREGym/.env || true",
                ],
                check=True,
                env=env,
            )
        except subprocess.CalledProcessError as e:
            print(f"FAILED: {host} ({e})")


def _brew_shellenv_cmd() -> str:
    if Path("/home/linuxbrew/.linuxbrew/bin/brew").exists():
        return 'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"'
    return 'eval "$(brew shellenv)"'


def _install_brew_if_needed():
    for node in _read_nodes("nodes.txt"):
        if _brew_exists(node):
            print(f"[{node}] Homebrew already installed.")
            continue

        print(f"[{node}] Installing Homebrew (non-interactive)...")
        remote_cmd = (
            "tmux new-session -d -s install_brew "
            '\'bash -ic "NONINTERACTIVE=1 /bin/bash -c \\"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\\"; sleep infinity"\''
        )

        subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                node,
                remote_cmd,
            ],
            env=ENV,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=True,
        )
        print(f"[{node}] Homebrew installed.")


def install_python():
    try:
        _install_brew_if_needed()
        shellenv = _brew_shellenv_cmd()
        subprocess.run(
            ["bash", "-ic", f"{shellenv}; brew --version; brew install python@3.12"],
            env=ENV,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=True,
        )
        print("Python installed successfully.")
    except subprocess.TimeoutExpired:
        print("Timed out installing Python.")
    except subprocess.CalledProcessError as e:
        print(f"Error installing Python: exit {e.returncode}")


def _resolve_kind_config() -> str | None:
    kind_dir = SREGYM_ROOT / "kind"
    prefs = [
        kind_dir / "kind-config-x86.yaml",
        kind_dir / "kind-config-arm.yaml",
    ]
    for p in prefs:
        if p.is_file():
            return str(p)
    if kind_dir.is_dir():
        for p in sorted(kind_dir.glob("*.yaml")):
            if p.is_file():
                return str(p)
    return None


def create_cluster(user):
    for node in _read_nodes("nodes.txt"):
        print(f"\n=== [Create Kind Cluster] {node} ===")
        TMUX_SESSION = "cluster_setup"

        cmd = f'ssh -o StrictHostKeyChecking=no {node} "bash -ic \\"tmux new-session -d -s cluster_setup \'kind create cluster --config /users/{user}/SREGym/kind/kind-config-x86.yaml; sleep infinity\'\\""'

        subprocess.run(
            cmd,
            check=True,
            shell=True,
            executable="/bin/zsh",
        )


def copy_env():
    for node in _read_nodes("nodes.txt"):
        print(f"\n=== [SCP .env] {node} ===")
        subprocess.run(
            [
                "scp",
                "-o",
                "StrictHostKeyChecking=accept-new",
                str(LOCAL_ENV),
                f"{node}:~/SREGym/.env",
            ],
            check=True,
        )
        subprocess.run(
            [
                "ssh",
                "-A",
                "-o",
                "StrictHostKeyChecking=accept-new",
                node,
                "sed -i '/^API_KEY.*/d' ~/SREGym/.env || true",
            ],
            check=True,
        )


def install_kubectl():
    _install_brew_if_needed()
    print("installed brew")
    SCRIPTS_DIR = Path.home() / "e2e-testing-scripts"

    for node in _read_nodes("nodes.txt"):
        print(f"\n=== [Install kubectl] {node} ===")
        cmd = f'ssh -o StrictHostKeyChecking=no {node} "bash -ic \\"brew install kubectl helm\\""'
        subprocess.run(
            cmd,
            check=True,
            shell=True,
            executable="/bin/zsh",
        )
    print("Kubectl installed successfully on all nodes.")


def set_up_environment():
    try:
        shellenv = _brew_shellenv_cmd()
        subprocess.run(
            ["bash", "-ic", f"{shellenv}; command -v uv || brew install uv"],
            env=ENV,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=True,
        )
    except Exception:
        pass
    nodes = _read_nodes("nodes.txt")
    commands = [
        f"cd ~/SREGym",
        'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"',
        "command -v uv >/dev/null 2>&1 || brew install uv",
        'uv venv -p "$(command -v python3.12 || command -v python3)"',
        "source .venv/bin/activate",
        "uv sync",
    ]
    cmd = " && ".join(commands)
    print(f"\n==> RUN: {cmd}")
    try:
        subprocess.run(
            cmd,
            shell=True,
            executable="/bin/zsh",
            env=ENV,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=True,
        )
        print("Setup completed successfully!")
    except subprocess.TimeoutExpired:
        print("Setup timed out.")
    except subprocess.CalledProcessError as e:
        print(f"Setup failed with return code {e.returncode}")


def collect_logs(user, nodes_file: str = "nodes.txt"):
    """
    Copy log files from all nodes into a local folder called 'node_logs'.

    Parameters:
        nodes_file (str): Path to the file listing nodes (one per line).
        remote_log (str): Path to the log file on the remote machine.
    """
    local_dir = Path("./node_logs")
    local_dir.mkdir(exist_ok=True)
    today = date.today().strftime("%Y-%m-%d")
    day = "$(date +%Y-%m-%d)"
    remote_log = f"/users/{user}/SREGym/global_benchmark_*.txt"

    nodes = _read_nodes(nodes_file)

    for node in nodes:
        node_name = node.split("@")[-1]

        local_path = local_dir / node_name
        local_path.mkdir(exist_ok=True)

        print(f"Copying {remote_log} from {node} -> {local_path}")
        cmd = [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            f"{node}:{remote_log}",
            str(local_path),
        ]

        try:
            subprocess.run(cmd, check=True)
            print(f"Copied log from {node}")
        except subprocess.CalledProcessError:
            print(f"Failed to copy log from {node} (file may not exist)")


def kill_server():
    TMUX_KILL_CMD = "tmux kill-server"
    for host in _read_nodes("nodes.txt"):
        print(f"\n=== [KILL TMUX SESSIONS] {host} ===")
        _run(["ssh", host, TMUX_KILL_CMD])


if __name__ == "__main__" and "--installations" in sys.argv:
    installations()
    sys.exit(0)

if __name__ == "__main__" and "--setup-env" in sys.argv:
    set_up_environment()
    sys.exit(0)

if __name__ == "__main__":
    user = input("Please enter your username to continue: ").strip()
    # initialize global variables
    init_user_paths(user)

    # kills any existing tmux servers
    kill_server()

    # copying all scripts
    scp_scripts_to_all(user, "nodes.txt")
    # clone repo
    clone(nodes_file="nodes.txt")
    # comment out problems that we don't test
    comment_out_problems()

    # installs prereqs
    run_installations_all(user, "nodes.txt")
    sleep(120)
    install_kubectl()
    create_cluster(user)
    sleep(120)
    copy_env()
    # set up python environment
    run_setup_env_all(user, "nodes.txt")
    sleep(120)
    # runs auto submitting script to keep benchmark going
    run_submit(user, "nodes.txt")
    # collects any logs
    collect_logs(user, "nodes.txt")
