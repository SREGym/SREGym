import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
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
        _run(["scp", "-r", "-o", "StrictHostKeyChecking=no", LOCAL_COPY_SRC, f"{host}:~"])


REMOTE_SELF_PATH = "scripts/automating_tests.py"


# def run_installations_all(nodes_file: str = "nodes.txt"):
#     """SSH each node and run this file with --installations."""
#     for host in _read_nodes(nodes_file):
#         print(f"\n=== [SSH install] {host} ===")
#         _run(["ssh", host, f"bash -lc 'python3 {REMOTE_SELF_PATH} --installations'"])


def run_installations_all(nodes_file: str = "nodes.txt"):
    """SSH each node and run this file with --installations in a tmux session named 'installations'."""
    session = "installations"
    tmux_cmd = (
        f"if tmux has-session -t {session}; then tmux kill-session -t {session}; fi; "
        f"tmux new-session -d -s {session} "
        f"'bash -ic \"python3 {REMOTE_SELF_PATH} --installations; sleep infinity\"'"
    )
    for host in _read_nodes(nodes_file):
        _run(["ssh", host, tmux_cmd])


def run_setup_env_all(nodes_file: str = "nodes.txt"):
    """SSH each node and run this file with --setup-env in a detached tmux session."""
    for host in _read_nodes(nodes_file):
        print(f"\n=== [SSH setup-env] {host} ===")

        remote_tmux = (
            "tmux kill-session -t setup_env 2>/dev/null || true; "
            "tmux new-session -d -s setup_env "
            "'bash -ic \""
            "cd ~/scripts && "
            "python3 automating_tests.py --setup-env 2>&1 | tee -a ~/setup_env_log.txt; "
            "sleep infinity\"'"
        )

        _run(["ssh", host, remote_tmux])
        print(f"✅ Started tmux session 'setup_env' on {host} (log: ~/setup_env_log.txt)")


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


# make it take parameter node
def _brew_exists(node: str) -> bool:
    """Check if Homebrew is installed on a remote node via SSH."""
    try:
        subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                node,
                "bash -lc 'command -v brew >/dev/null 2>&1'",
            ],
            env=ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


# def _brew_exists() -> bool:
#     try:
#         subprocess.run(
#             "brew",
#             env=ENV,
#             stdin=subprocess.DEVNULL,
#             stdout=subprocess.DEVNULL,
#             stderr=subprocess.DEVNULL,
#             check=True,
#         )
#         return True
#     except subprocess.CalledProcessError:
#         return False


def read_file(file_path: Path) -> list[str]:
    with open(file_path) as f:
        res = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    return res


# def comment_out_problems():
#     nodes = _read_nodes("nodes.txt")
#     problems = read_file("registry.txt")

#     for node in nodes:
#         print(f"\n=== [Comment out problems on {node}] ===")

#         remote_py = r"""
# import re, pathlib, json

# p = pathlib.Path('~/SREGym/sregym/conductor/problems/registry.py').expanduser()
# backup = p.with_suffix('.py.bak')
# backup.write_text(p.read_text())  # create a backup before modifying
# lines = p.read_text().splitlines()

# keys = set({PROBLEMS_JSON})
# out = []
# in_registry = False
# brace_depth = 0
# commenting = False
# open_parens = 0

# for line in lines:
#     stripped = line.strip()

#     # Detect start of PROBLEM_REGISTRY dict
#     if not in_registry and stripped.startswith("self.PROBLEM_REGISTRY") and stripped.endswith("{"):
#         in_registry = True
#         brace_depth = 1
#         out.append(line)
#         continue

#     if in_registry:
#         # Update brace nesting count
#         brace_depth += line.count("{") - line.count("}")

#         # If we've closed the registry dictionary, exit
#         if brace_depth <= 0:
#             in_registry = False
#             commenting = False
#             out.append(line)
#             continue

#         # If currently commenting
#         if commenting:
#             out.append("#" + line)
#             open_parens += line.count("(") - line.count(")")
#             # stop commenting when parentheses close and line ends with comma
#             if open_parens <= 0 and line.strip().endswith(","):
#                 commenting = False
#             continue

#         # Check for a problem key that matches one from registry.txt
#         for key in keys:
#             if re.search(rf'"{re.escape(key)}"\s*:', line):
#                 commenting = True
#                 open_parens = line.count("(") - line.count(")")
#                 out.append("#" + line)
#                 break
#         else:
#             out.append(line)
#     else:
#         out.append(line)

# p.write_text("\n".join(out) + "\n")
# print("✅ Finished safely. Backup at:", backup)
# """.replace("{PROBLEMS_JSON}", json.dumps(problems))

#         quoted_py = shlex.quote(remote_py)

#         cmd = [
#             "ssh",
#             "-o", "StrictHostKeyChecking=no",
#             node,
#             "python3",
#             "-c",
#             quoted_py,
#         ]

#         try:
#             subprocess.run(cmd, check=True)
#         except subprocess.CalledProcessError as e:
#             print(f"❌ Failed on {node}: {e}")


def comment_out_problems():
    nodes = _read_nodes("nodes.txt")
    problems = read_file("registry.txt")
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


def clone(nodes_file: str = "nodes.txt", user: str = "lilygn", repo: str = "git@github.com:SREGym/SREGym.git"):
    """
    Clone the repo on all remote nodes using local SSH agent forwarding.
    """
    env = os.environ.copy()
    if "SSH_AUTH_SOCK" not in env or not env["SSH_AUTH_SOCK"]:
        raise EnvironmentError("No SSH agent detected. Run `ssh-add -l` to confirm your key is loaded.")

    REMOTE_CMD = f'GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" git clone --recurse-submodules {repo} && cd SREGym && git checkout lily-e2e-test'

    with open(nodes_file) as f:
        nodes = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    for host in nodes:
        print(f"=== {host} ===")
        cmd = [
            "ssh",
            "-A",  # crucial: forward your local SSH agent
            "-o",
            "StrictHostKeyChecking=no",
            host,
            REMOTE_CMD,
        ]
        try:
            subprocess.run(cmd, check=True, env=env)
            subprocess.run(
                ["scp", "-o", "StrictHostKeyChecking=accept-new", str(LOCAL_ENV), f"{host}:~/SREGym/.env"],
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


# copy .ssh folder onto the machine
def create_cluster():

    for node in _read_nodes("nodes.txt"):
        print(f"\n=== [Create Kind Cluster] {node} ===")
        TMUX_SESSION = "cluster_setup"
        # cmd = [
        #     "ssh",
        #     "-o",
        #     "StrictHostKeyChecking=no",
        #     node,
        #     f"tmux new-session -d -s {TMUX_SESSION} "
        #     f"'kind create cluster --config {shlex.quote(cfg)}; "
        #     f"sleep infinity'",
        # ]
        cmd = f'ssh -o StrictHostKeyChecking=no {node} "bash -ic \\"tmux new-session -d -s cluster_setup \'kind create cluster --config /users/lilygn/SREGym/kind/kind-config-x86.yaml; sleep infinity\'\\""'
        # cmd = f'ssh -o StrictHostKeyChecking=no {node} "bash -ic ls"'

        subprocess.run(
            cmd,
            check=True,
            shell=True,
            executable="/bin/zsh",
        )

        #     cmd = [
        #         "ssh",
        #         "-o",
        #         "StrictHostKeyChecking=no",
        #         node,
        #         f"tmux new-session -d -s cluster-setup " f"'kind create cluster; " f"sleep infinity'",
        #     ]
        #     subprocess.run(
        #         cmd,
        #         check=True,
        #         cwd=str(SREGYM_ROOT),
        #     )


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


def install_kubectl():

    _install_brew_if_needed()
    print("installed brew")
    SCRIPTS_DIR = Path.home() / "scripts"

    for node in _read_nodes("nodes.txt"):
        print(f"\n=== [Install kubectl] {node} ===")
        # cmd2 = (  f"ssh -o StrictHostKeyChecking=no {node} "
        #     "\"bash -lc 'cd ~/scripts && chmod +x brew.sh && bash brew.sh'\"")
        #         cmd = (
        #     f'ssh -o StrictHostKeyChecking=no {node} '
        #     '"tmux new-session -d -s installations_kubectl '
        #     '\'bash -lc \\\"brew install kubectl helm\\\"\'"'
        # )
        # SECOND VERSION:
        #     cmd = (
        # f'ssh -o StrictHostKeyChecking=no {node} '
        # '"tmux new-session -d -s install_kubectl '
        # '\'bash -lc \\"brew install kubectl helm\\"; sleep infinity\'"'
        #     )
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
            ["bash", "-ic", f"{shellenv}; command -v uv || brew install uv || python3 -m pip install --user uv"],
            env=ENV,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=True,
        )
    except Exception:
        pass
    nodes = _read_nodes("nodes.txt")
    # TMUX_SESSION = "cluster_setup"
    # create_cluster()
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
    clone()
    comment_out_problems()
    kill_server()

    run_installations_all("nodes.txt")
    install_kubectl()
    create_cluster()

    copy_env()
    run_setup_env_all("nodes.txt")

    kill_server()
    run_submit()
