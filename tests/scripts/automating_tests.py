import os
import shlex
import subprocess
import sys
from pathlib import Path
from time import sleep

# we added the ssh key to the ssh agent such that all of all the keys are carried with the ssh connection.

SREGYM_DIR = Path("/users/lilygn/SREGym").resolve()
LOCAL_ENV = Path("/Users/lilygniedz/Documents/SREArena/SREArena/.env")

SREGYM_ROOT = Path("/users/lilygn/SREGym").resolve()
KIND_DIR = SREGYM_ROOT / "kind"
REMOTE_ENV = "/users/lilygn/SREGym/.env"
ENV = {
    **os.environ,
    "CI": "1",
    "NONINTERACTIVE": "1",
    "DEBIAN_FRONTEND": "noninteractive",
    "SUDO_ASKPASS": "/bin/false",
}
TIMEOUT = 1800

# commands = [
#     f"cd {shlex.quote(str(SREGYM_DIR))}",
#     "uv venv -p $(which python3.12)",
#     "source .venv/bin/activate",
#     "uv sync",
#     "cd ..",
#     #"cd SREGym",
# ]
commands = [
    "cd /users/lilygn/SREGym",
    'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"',
    "command -v uv >/dev/null 2>&1 || brew install uv || python3 -m pip install --user uv",
    'uv venv -p "$(command -v python3.12 || command -v python3)"',
    "source .venv/bin/activate",
    "uv sync",
]

scripts = [
    "brew.sh",
    "go.sh",
    "docker.sh",
    "kind.sh",
    "kubectl.sh",
]


def _read_nodes(path: str = "nodes.txt") -> list[str]:
    base = Path(__file__).resolve().parent  # directory of this script
    full_path = (base / path).resolve()
    if not full_path.exists():
        raise FileNotFoundError(f"nodes.txt not found at {full_path}")
    with open(full_path) as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def _run(cmd: list[str]):
    print("$", " ".join(shlex.quote(x) for x in cmd))
    subprocess.run(cmd)


def scp_scripts_to_all(nodes_file: str = "nodes.txt"):
    """scp -r LOCAL_COPY_SRC -> ~/scripts on each node."""
    LOCAL_COPY_SRC = "/Users/lilygniedz/Documents/SREArena/SREArena/tests/scripts"

    if not Path(LOCAL_COPY_SRC).exists():
        raise FileNotFoundError(f"LOCAL_COPY_SRC not found: {LOCAL_COPY_SRC}")
    for host in _read_nodes(nodes_file):
        print(f"\n=== [SCP] {host} ===")
        _run(["scp", "-r", "-o", "StrictHostKeyChecking=no", LOCAL_COPY_SRC, f"{host}:~"])


REMOTE_SELF_PATH = "scripts/automating_tests.py"


def run_installations_all(nodes_file: str = "nodes.txt"):
    """SSH each node and run this file with --installations."""
    for host in _read_nodes(nodes_file):
        print(f"\n=== [SSH install] {host} ===")
        _run(["ssh", host, f"bash -lc 'python3 {REMOTE_SELF_PATH} --installations'"])


def run_setup_env_all(nodes_file: str = "nodes.txt"):
    """SSH each node and run this file with --setup-env."""
    for host in _read_nodes(nodes_file):
        print(f"\n=== [SSH setup-env] {host} ===")
        _run(
            [
                "ssh",
                host,
                # add eval brew shellenv before running python
                'bash -lc \'eval "$($(brew --prefix 2>/dev/null || echo /home/linuxbrew/.linuxbrew)/bin/brew shellenv)"; '
                "cd ~ && python3 scripts/automating_tests.py --setup-env'",
            ]
        )


def run_shell_command(path: Path):
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
    SCRIPTS_DIR = Path.home() / "scripts"
    for script in scripts:
        path = SCRIPTS_DIR / script
        if path.exists():
            run_shell_command(path)
        else:
            print(f"Script {script} not found at {path}")
            return
    install_python()
    install_git()


def _brew_exists() -> bool:
    # for p in (
    #     "/home/linuxbrew/.linuxbrew/bin/brew",
    #     "/opt/homebrew/bin/brew",
    #     "/usr/local/bin/brew",
    # ):
    #     if Path(p).exists():
    #         return True
    try:
        subprocess.run(
            "brew",
            env=ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def read_file(file_path: Path) -> list[str]:
    with file_path.open("r", encoding="utf-8") as f:
        return f.readlines()


def comment_out_problems():
    nodes = _read_nodes("nodes.txt")
    problems = read_file("problems.txt")
    problem_count = len(problems) / len(nodes)
    mapping = {}
    m = len(problems)
    n = len(nodes)
    for i, node in enumerate(nodes):
        start = i * m // n
        end = (i + 1) * m // n
        mapping[node] = problems[start:end]
    for node, probs in mapping.items():
        for prob in probs:
            print(f"On node {node}, comment out line: {prob.strip()}")
            cmd = f'ssh -o StrictHostKeyChecking=no {node} "sed -i \'/\\"{prob}\\":/s/^/#/\' ~/SREGym/sregym/conductor/problems/registry.py"'
            subprocess.run(cmd, shell=True, check=True)


def run_submit(nodes_file: str = "nodes.txt"):
    TMUX_CMD = (
        "tmux kill-session -t submission 2>/dev/null || true; "
        "tmux new-session -d -s submission -c /users/$USER/scripts "
        "'python3 auto_submit.py 2>&1 | tee -a ~/submission_log.txt; sleep infinity'"
    )

    # TMUX_CMD = "tmux new-session -d -s showcase_tmux2 'cd /users/lilygn/scripts && python3 auto_submit.py 2>&1 | tee submission_log.txt; sleep infinity;'"
    # TMUX_CMD2 = "tmux new-session -d -s main_tmux 'bash -c \"cd /users/lilygn/SREGym && /users/lilygn/SREGym/.venv/bin/python3 main.py 2>&1 | tee global_benchmark_log.txt;\" sleep infinity;'"
    # TMUX_CMD2 = "tmux new-session -d -s main_tmux 'echo $PATH; sleep infinity;'"
    TMUX_CMD2 = (
        "tmux new-session -d -s main_tmux "
        "'env -i PATH=/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:/usr/local/bin:/usr/bin:/bin "
        "HOME=$HOME TERM=$TERM "
        'bash -lc "echo PATH=\\$PATH; '
        "command -v kubectl; kubectl version --client --short || true; "
        "command -v helm || true; "
        "cd /users/lilygn/SREGym && "
        "/users/$USER/SREGym/.venv/bin/python3 main.py 2>&1 | tee -a global_benchmark_Log_$(date +%Y-%m-%d).txt; "
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
            ["bash", "-lc", f"{shellenv}; brew --version; brew install git"],
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


def clone(nodes_file: str = "nodes.txt", user: str = "lilygn", repo: str = "git@github.com:xlab-uiuc/SREGym.git"):
    REMOTE_CMD = f'GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=no" git clone --recurse-submodules {repo}'

    with open(nodes_file) as f:
        nodes = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    for host in nodes:
        print(f"=== {host} ===")
        cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            # "-o", "IdentitiesOnly=yes",
            host,
            f"{REMOTE_CMD}",
        ]

        # try:
        #     #subprocess.run(cmd, check=True)
        #     subprocess.run(
        #     ["scp", "-o", "StrictHostKeyChecking=no", str(LOCAL_ENV), f"{host}:$HOME/SREGym/.env"], check=True
        # )
        #     subprocess.run(
        #         ["ssh", "-o", "StrictHostKeyChecking=no", host, "sed -i '/^API_KEY.*/d' /users/lilygn/SREGym/.env"],
        #         check=True,
        #     )
        # except subprocess.CalledProcessError:
        #     print(f"FAILED: {host}")
        try:
            subprocess.run(cmd, check=True)
            subprocess.run(
                ["scp", "-o", "StrictHostKeyChecking=accept-new", str(LOCAL_ENV), f"{host}:~/SREGym/.env"], check=True
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
            )
        except subprocess.CalledProcessError:
            print(f"FAILED: {host}")


def copy_env():
    for node in _read_nodes("nodes.txt"):
        print(f"\n=== [SCP .env] {node} ===")
        subprocess.run(
            ["scp", "-o", "StrictHostKeyChecking=accept-new", str(LOCAL_ENV), f"{node}:~/SREGym/.env"], check=True
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


def _brew_shellenv_cmd() -> str:
    if Path("/home/linuxbrew/.linuxbrew/bin/brew").exists():
        return 'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"'
    return 'eval "$(brew shellenv)"'


def _install_brew_if_needed():
    if _brew_exists():
        print("------Homebrew already installed.")
        return
    print("Homebrew not found — installing non-interactively for Linux/macOS…")
    for node in _read_nodes("nodes.txt"):
        print(f"\n=== [Install Homebrew] {node} ===")
        remote_cmd = 'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
        cmd = f"ssh -o StrictHostKeyChecking=no {node} '{remote_cmd}'"

        subprocess.run(
            ["bash", "-lc", cmd],
            env=ENV,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=True,
        )
    print("Homebrew installed.")


def install_python():
    try:
        _install_brew_if_needed()
        shellenv = _brew_shellenv_cmd()
        subprocess.run(
            ["bash", "-lc", f"{shellenv}; brew --version; brew install python@3.12"],
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


def create_cluster():
    cfg = _resolve_kind_config()
    if cfg:
        subprocess.run(
            ["bash", "-lc", f"kind delete cluster || true; kind create cluster --config {shlex.quote(cfg)}"],
            check=True,
            cwd=str(SREGYM_ROOT),
        )
    else:
        subprocess.run(
            ["bash", "-lc", "kind delete cluster || true; kind create cluster"],
            check=True,
            cwd=str(SREGYM_ROOT),
        )


# def create_cluster():
#     try:
#         subprocess.run(["kind", "create", "cluster", "--config", "kind/kind-config-x86.yaml"], check=True, cwd="/users/lilygn/SREGym",)
#         print("Kubernetes cluster created successfully.")
#     except subprocess.TimeoutExpired:
#         print("Timed out creating Kubernetes cluster.")
#     except subprocess.CalledProcessError as e:
#         print(f"Error creating Kubernetes cluster: exit {e.returncode}")


def install_kubectl():

    _install_brew_if_needed()
    print("installed brew")
    SCRIPTS_DIR = Path.home() / "scripts"

    for node in _read_nodes("nodes.txt"):
        print(f"\n=== [Install kubectl] {node} ===")
        # cmd2 = (  f"ssh -o StrictHostKeyChecking=no {node} "
        #     "\"bash -lc 'cd ~/scripts && chmod +x brew.sh && bash brew.sh'\"")

        cmd = f'ssh -o StrictHostKeyChecking=no {node} "bash -ic \\"brew install kubectl helm\\""'

        # cmd3 = (
        #     f"ssh -o StrictHostKeyChecking=no {node} \"bash -ic \\\"echo $PATH\\\"\""
        # )
        print(f"WHAT IS PATH??")
        subprocess.run(
            cmd,
            check=True,
            shell=True,
            executable="/bin/zsh",
        )
        # subprocess.run(cmd2, shell=True, check=True)
        # subprocess.run(cmd, shell=True, check=True)
    print("Kubectl installed successfully on all nodes.")


def set_up_environment():
    try:
        shellenv = _brew_shellenv_cmd()
        subprocess.run(
            ["bash", "-lc", f"{shellenv}; command -v uv || brew install uv || python3 -m pip install --user uv"],
            env=ENV,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=True,
        )
    except Exception:
        pass
    nodes = _read_nodes("nodes.txt")
    TMUX_SESSION = "cluster_setup"
    for node in nodes:
        print(f"\n=== [SET UP ENV] {node} ===")
        cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            node,
            f"tmux new-session -d -s {TMUX_SESSION} "
            f"'cd ~/SREGym && kind create cluster --config kind/kind-config-arm.yaml; "
            f"sleep infinity'",
        ]
        subprocess.run(cmd, check=True)
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
    scp_scripts_to_all("nodes.txt")
    # clone()
    comment_out_problems()
    install_kubectl()
    run_installations_all("nodes.txt")
    copy_env()
    run_setup_env_all("nodes.txt")

    # install_kubectl()
    kill_server()
    run_submit()

# if __name__ == "__main__":
#     run_submit()
