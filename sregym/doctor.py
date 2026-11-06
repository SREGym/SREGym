"""Check that this machine can run a benchmark before a run starts.

A benchmark run discovers most setup problems late: a kind node without
/run/udev fails during OpenEBS setup, a cluster without the Calico
adminnetworkpolicy tier fails when the egress boundary is installed, and a
missing judge credential fails after the agent image is built. ``doctor``
runs those same checks up front, in a few seconds, and says how to fix each
one. It does not create, change, or delete anything.

Run it with the flags you would pass to a benchmark run::

    uv run main.py --doctor --agent codex --model gpt-5 --profile svelte
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sregym.profile import SVELTE

REPO_ROOT = Path(__file__).resolve().parent.parent

# docs/SREGym-Lite.md: minimum machine for the Lite suite on kind.
MIN_CPUS = 8
MIN_MEMORY_GB = 16
MIN_DISK_GB = 100

# Host ports a run listens on, with the variable that moves each one.
HOST_PORTS = (("API_PORT", 8000), ("MCP_SERVER_PORT", 9954), ("K8S_PROXY_PORT", 16443))

# Agents that authenticate with a CLI subscription rather than a LiteLLM model key.
SUBSCRIPTION_CREDENTIALS = {
    "codex": ("OPENAI_API_KEY",),
    "claudecode": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    "claudecode_radar": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    "copilot": ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
    "cursor": ("CURSOR_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"),
}
# Agents that need no model at all.
MODEL_FREE_AGENTS = {"autosubmit", "debug", "demo"}


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    fix: str = ""


@dataclass
class DoctorOptions:
    agent: str = "stratus"
    model: str = "gpt-5"
    judge_model: str | None = None
    judge_backend: str = "api"
    profile: str = "full"
    internet_access: str = "filtered"
    use_external_harness: bool = False
    live_judge: bool = True
    environment: dict[str, str] = field(default_factory=lambda: dict(os.environ))


Runner = Callable[[list[str], float], tuple[int, str]]


def run_command(argv: list[str], timeout: float = 20) -> tuple[int, str]:
    """Run a read-only command; never raise for a missing tool or a hang."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"{argv[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, f"{shlex.join(argv)} timed out after {timeout:g}s"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


class Doctor:
    def __init__(self, options: DoctorOptions, runner: Runner = run_command, repo_root: Path = REPO_ROOT):
        self.options = options
        self.run = runner
        self.repo_root = repo_root
        self.env = options.environment
        self._docker_ok: bool | None = None
        self._cluster_nodes: list[dict] | None = None

    # ----------------------------------------------------------------- tooling

    def check_uv(self) -> CheckResult:
        required = _required_uv_version(self.repo_root / "pyproject.toml")
        code, out = self.run(["uv", "--version"], 10)
        if code != 0:
            return CheckResult("uv", Status.FAIL, "uv is not installed", "Install uv: https://docs.astral.sh/uv/")
        match = re.search(r"(\d+\.\d+\.\d+)", out)
        version = match.group(1) if match else "unknown"
        if required and match and not _satisfies(version, required):
            return CheckResult("uv", Status.FAIL, f"uv {version} (project requires {required})", "uv self update")
        return CheckResult("uv", Status.OK, f"uv {version}" + (f" (requires {required})" if required else ""))

    def check_submodule(self) -> CheckResult:
        apps = self.repo_root / "SREGym-applications"
        if apps.is_dir() and any(apps.iterdir()):
            return CheckResult("Applications submodule", Status.OK, "SREGym-applications is checked out")
        return CheckResult(
            "Applications submodule",
            Status.FAIL,
            "SREGym-applications is empty, so no application can be deployed",
            "git submodule update --init --recursive",
        )

    # ------------------------------------------------------------------ docker

    def check_docker(self) -> list[CheckResult]:
        code, out = self.run(["docker", "info", "--format", "{{json .}}"], 20)
        if code != 0:
            self._docker_ok = False
            fix = "Install Docker" if code == 127 else "Start the Docker daemon (or Docker Desktop/OrbStack)"
            return [CheckResult("Docker", Status.FAIL, _first_line(out) or "Docker is not reachable", fix)]
        self._docker_ok = True
        try:
            info = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return [CheckResult("Docker", Status.OK, "Docker daemon is running")]

        results = [CheckResult("Docker", Status.OK, f"Docker {info.get('ServerVersion', '')} is running".strip())]
        cpus = int(info.get("NCPU") or 0)
        memory_gb = int(info.get("MemTotal") or 0) / 1024**3
        detail = f"{cpus} CPUs, {memory_gb:.1f} GB memory available to Docker"
        if cpus >= MIN_CPUS and memory_gb >= MIN_MEMORY_GB * 0.95:
            results.append(CheckResult("Machine size", Status.OK, detail))
        else:
            fix = f"SREGym-Lite needs at least {MIN_CPUS} CPUs and {MIN_MEMORY_GB} GB."
            host_gb = _host_memory_gb()
            if _is_docker_vm(info) and host_gb and host_gb > memory_gb * 1.5:
                detail += f" (this host has {host_gb:.0f} GB, but Docker's VM is capped)"
                fix = (
                    f"Raise the memory limit in {info.get('OperatingSystem')} settings (Resources); "
                    "kind runs inside that VM. " + fix
                )
            if self.options.profile != SVELTE:
                fix += " On a smaller machine try --profile svelte."
            results.append(CheckResult("Machine size", Status.WARN, detail, fix))

        root = info.get("DockerRootDir")
        if root and Path(root).exists():
            try:
                free_gb = shutil.disk_usage(root).free / 1024**3
            except OSError:
                free_gb = None
            if free_gb is not None:
                status = Status.OK if free_gb >= MIN_DISK_GB * 0.5 else Status.WARN
                fix = "" if status == Status.OK else f"Free disk space; images and volumes need up to {MIN_DISK_GB} GB"
                results.append(CheckResult("Disk space", status, f"{free_gb:.0f} GB free under {root}", fix))
        return results

    def check_agent_image(self) -> CheckResult:
        if not self._docker_ok:
            return CheckResult("Agent image", Status.SKIP, "Docker is not reachable")
        from sregym.service.container_runner import DEFAULT_AGENT_IMAGE

        image = self.env.get("SREGYM_AGENT_IMAGE") or DEFAULT_AGENT_IMAGE
        code, _ = self.run(["docker", "image", "inspect", image], 20)
        short = image.split("@")[0]
        if code == 0:
            return CheckResult("Agent image", Status.OK, f"{short} is available locally")
        return CheckResult(
            "Agent image",
            Status.WARN,
            f"{short} is not pulled yet; the first run will download it",
            f"Pre-pull it to save time: docker pull {image}",
        )

    # ----------------------------------------------------------------- cluster

    def check_cluster(self) -> CheckResult:
        code, out = self.run(["kubectl", "get", "nodes", "-o", "json"], 20)
        if code == 127:
            self._cluster_nodes = []
            return CheckResult("Kubernetes cluster", Status.FAIL, "kubectl is not installed", "Install kubectl")
        if code != 0:
            self._cluster_nodes = []
            return self._diagnose_unreachable_cluster(out)
        try:
            nodes = json.loads(out).get("items", [])
        except ValueError:
            nodes = []
        self._cluster_nodes = nodes
        ready = [n for n in nodes if _node_ready(n)]
        context = _first_line(self.run(["kubectl", "config", "current-context"], 10)[1])
        detail = f"{len(ready)}/{len(nodes)} nodes Ready" + (f" (context {context})" if context else "")
        if nodes and len(ready) == len(nodes):
            return CheckResult("Kubernetes cluster", Status.OK, detail)
        return CheckResult("Kubernetes cluster", Status.FAIL, detail, "kubectl get nodes; kubectl describe node <name>")

    def _diagnose_unreachable_cluster(self, error: str) -> CheckResult:
        name = "Kubernetes cluster"
        create = "Create the supported cluster: kind/setup_kind_cluster.sh (see kind/README.md)"
        context = _first_line(self.run(["kubectl", "config", "current-context"], 10)[1])
        if not context or "not set" in context:
            return CheckResult(name, Status.FAIL, "kubectl has no current context", create)
        detail = f"context {context}: {_summarize_kubectl_error(error)}"
        if context.startswith("kind-"):
            cluster = context.removeprefix("kind-")
            code, out = self.run(["kind", "get", "clusters"], 15)
            if code == 0 and cluster not in {line.strip() for line in out.splitlines()}:
                return CheckResult(
                    name, Status.FAIL, f"context {context} points to a kind cluster that no longer exists", create
                )
            code, out = self.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"label=io.x-k8s.kind.cluster={cluster}",
                    "--format",
                    "{{.Names}} {{.State}}",
                ],
                15,
            )
            stopped = [line.split()[0] for line in out.splitlines() if line.strip() and "running" not in line]
            if code == 0 and stopped:
                return CheckResult(
                    name,
                    Status.FAIL,
                    f"{detail}; kind node containers are stopped",
                    "docker start " + " ".join(stopped),
                )
        return CheckResult(name, Status.FAIL, detail, "kubectl cluster-info; or " + create[0].lower() + create[1:])

    def check_udev(self) -> CheckResult:
        name = "/run/udev on kind nodes"
        if self.options.profile == SVELTE:
            return CheckResult(name, Status.SKIP, "not needed with --profile svelte (no node-disk-manager)")
        if not self._cluster_nodes or not self._docker_ok:
            return CheckResult(name, Status.SKIP, "needs a reachable cluster and Docker")
        missing, checked = [], 0
        for node in (n.get("metadata", {}).get("name", "") for n in self._cluster_nodes):
            if not node:
                continue
            code, out = self.run(["docker", "exec", node, "test", "-d", "/run/udev"], 10)
            if code == 0:
                checked += 1
            elif "No such container" in out or code == 127:
                continue  # not a kind node; the check does not apply
            else:
                checked += 1
                missing.append(node)
        if not checked:
            return CheckResult(name, Status.SKIP, "cluster nodes are not kind containers")
        if missing:
            return CheckResult(
                name,
                Status.FAIL,
                f"missing on {', '.join(missing)}; OpenEBS node-disk-manager will not start",
                "Recreate the cluster with kind/kind-config.yaml (it mounts /run/udev), or use --profile svelte",
            )
        return CheckResult(name, Status.OK, f"mounted on all {checked} kind nodes")

    def check_calico_tier(self) -> CheckResult:
        name = "Calico adminnetworkpolicy tier"
        if self.options.internet_access != "filtered" or self.options.use_external_harness:
            return CheckResult(name, Status.SKIP, "only needed for --internet-access filtered")
        if not self._cluster_nodes:
            return CheckResult(name, Status.SKIP, "needs a reachable cluster")
        code, out = self.run(["kubectl", "get", "tiers.crd.projectcalico.org", "adminnetworkpolicy", "-o", "name"], 15)
        if code == 0:
            return CheckResult(name, Status.OK, "present; filtered internet access can be enforced")
        return CheckResult(
            name,
            Status.FAIL,
            "missing; filtered internet access needs Calico 3.29+",
            "Recreate the cluster with kind/setup_kind_cluster.sh, or run with --internet-access open",
        )

    # ------------------------------------------------------------- agent/model

    def check_agent(self) -> list[CheckResult]:
        from sregym.agent_registry import list_agents

        agents = list_agents(self.repo_root / "agents.yaml")
        reg = agents.get(self.options.agent)
        if reg is None:
            return [
                CheckResult(
                    "Agent",
                    Status.FAIL,
                    f"'{self.options.agent}' is not in agents.yaml",
                    "Choose one of: " + ", ".join(sorted(agents)),
                )
            ]
        results = [CheckResult("Agent", Status.OK, f"'{reg.name}' is registered")]
        if self.options.internet_access == "filtered" and not self.options.use_external_harness:
            results.append(self._check_filtered_provider(reg))
        results.append(self._check_agent_credentials())
        return results

    def _check_filtered_provider(self, reg) -> CheckResult:
        from sregym.service.internet_policy import InternetPolicy
        from sregym.service.provider_endpoints import provider_endpoint_rules

        name = "Filtered network allowlist"
        if not reg.container_isolation:
            return CheckResult(
                name, Status.FAIL, f"'{reg.name}' does not run in a container", "Use --internet-access open"
            )
        policy = InternetPolicy.from_mode("filtered", agent_name=reg.name, model_id=self.options.model)
        try:
            rules = provider_endpoint_rules(policy, self.env, codex_subscription_auth=_codex_logged_in(self.env))
        except ValueError as exc:
            return CheckResult(name, Status.FAIL, str(exc), "Set AGENT_API_BASE or use --internet-access open")
        hosts = sorted({getattr(r, "host", str(r)) for r in rules})
        return CheckResult(name, Status.OK, "agent may reach: " + (", ".join(hosts) or "local services only"))

    def _check_agent_credentials(self) -> CheckResult:
        name = "Agent credentials"
        agent, model = self.options.agent, self.options.model
        if agent in MODEL_FREE_AGENTS:
            return CheckResult(name, Status.SKIP, f"'{agent}' does not call a model")
        if self.env.get("AGENT_API_BASE"):
            if self.env.get("AGENT_API_KEY"):
                return CheckResult(name, Status.OK, f"AGENT_API_KEY set for {self.env['AGENT_API_BASE']}")
            return CheckResult(
                name, Status.FAIL, "AGENT_API_BASE is set but AGENT_API_KEY is not", "export AGENT_API_KEY=..."
            )
        if agent == "codex" and _codex_logged_in(self.env):
            return CheckResult(name, Status.OK, "Codex subscription login found")
        if agent in SUBSCRIPTION_CREDENTIALS:
            names = SUBSCRIPTION_CREDENTIALS[agent]
            if any(self.env.get(v) for v in names):
                return CheckResult(name, Status.OK, f"{next(v for v in names if self.env.get(v))} is set")
            fix = "codex login" if agent == "codex" else "export " + " or ".join(names)
            return CheckResult(name, Status.FAIL, f"no credentials found for '{agent}'", fix)
        return _litellm_credentials(name, model, self.env)

    def check_judge(self) -> CheckResult:
        name = "Judge"
        opts = self.options
        if opts.use_external_harness:
            return CheckResult(name, Status.SKIP, "not used with --use-external-harness")
        if opts.judge_backend != "api":
            from sregym.service.judge_runtime import _subscription_environment

            try:
                _subscription_environment(opts.judge_backend)
            except ValueError as exc:
                return CheckResult(
                    name, Status.FAIL, str(exc), f"Log in to {opts.judge_backend} or use --judge-backend api"
                )
            return CheckResult(name, Status.OK, f"{opts.judge_backend} subscription credentials found")

        judge_model = opts.judge_model or opts.model
        env = self.env
        base = env.get("JUDGE_API_BASE") or (None if opts.judge_model else env.get("AGENT_API_BASE"))
        key = env.get("JUDGE_API_KEY") or (None if opts.judge_model else env.get("AGENT_API_KEY"))
        if base:
            if not key:
                return CheckResult(
                    name, Status.FAIL, f"judge endpoint {base} has no API key", "export JUDGE_API_KEY=..."
                )
        else:
            creds = _litellm_credentials(name, judge_model, env)
            if creds.status == Status.FAIL:
                return creds
        if not opts.live_judge:
            return CheckResult(name, Status.OK, f"credentials found for {judge_model} (not contacted)")
        return self._ping_judge(judge_model)

    def _ping_judge(self, judge_model: str) -> CheckResult:
        """Send the same one-line request the run's judge preflight sends."""
        name = "Judge"
        try:
            from llm_backend.init_backend import get_llm_backend_for_judge

            previous = os.environ.get("JUDGE_MODEL_ID")
            os.environ["JUDGE_MODEL_ID"] = judge_model
            try:
                get_llm_backend_for_judge().inference("Say ok.", system_prompt="Reply with exactly 'ok'.")
            finally:
                if previous is None:
                    os.environ.pop("JUDGE_MODEL_ID", None)
                else:
                    os.environ["JUDGE_MODEL_ID"] = previous
        except Exception as exc:  # noqa: BLE001 - every failure here is a reportable finding
            return CheckResult(
                name,
                Status.FAIL,
                f"{judge_model} did not answer: {_first_line(str(exc))[:200]}",
                "Check credit/quota and --judge-model, or use --judge-backend codex",
            )
        return CheckResult(name, Status.OK, f"{judge_model} answered a test request")

    # ------------------------------------------------------------------- ports

    def check_ports(self) -> CheckResult:
        busy = []
        for variable, default in HOST_PORTS:
            port = int(self.env.get(variable) or default)
            if _port_in_use(port):
                busy.append(f"{port} ({variable})")
        if busy:
            return CheckResult(
                "Host ports",
                Status.WARN,
                "already in use: " + ", ".join(busy),
                "Stop the other process or set the listed variable to a free port",
            )
        return CheckResult("Host ports", Status.OK, "8000, 9954 and 16443 are free (or moved via env)")

    # --------------------------------------------------------------------- run

    def run_all(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        results.append(self.check_uv())
        results.append(self.check_submodule())
        results.extend(self.check_docker())
        results.append(self.check_agent_image())
        results.append(self.check_cluster())
        results.append(self.check_udev())
        results.append(self.check_calico_tier())
        results.extend(self.check_agent())
        results.append(self.check_judge())
        results.append(self.check_ports())
        return results


# ----------------------------------------------------------------------- helpers


def _first_line(text: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")


def _summarize_kubectl_error(text: str) -> str:
    """Reduce kubectl's multi-line client-go noise to the part a person needs."""
    match = re.search(r'Get \\?"(https?://[^/"\\]+)', text or "")
    server = match.group(1) if match else "the API server"
    for needle, message in (
        ("connection refused", f"{server} refused the connection"),
        ("i/o timeout", f"{server} did not answer"),
        ("no such host", f"{server} does not resolve"),
        ("Unauthorized", "credentials were rejected"),
        ("certificate", "TLS certificate was rejected"),
    ):
        if needle in (text or ""):
            return message
    return _first_line(text)[:160] or "not reachable"


def _host_memory_gb() -> float | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except (ValueError, OSError, AttributeError):
        return None


def _is_docker_vm(info: dict) -> bool:
    system = str(info.get("OperatingSystem", ""))
    return any(name in system for name in ("Docker Desktop", "OrbStack", "Colima", "Rancher Desktop"))


def _node_ready(node: dict) -> bool:
    for condition in node.get("status", {}).get("conditions", []):
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def _required_uv_version(pyproject: Path) -> str | None:
    try:
        data = tomllib.loads(pyproject.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return data.get("tool", {}).get("uv", {}).get("required-version")


def _satisfies(version: str, specifier: str) -> bool:
    try:
        from packaging.specifiers import SpecifierSet

        return version in SpecifierSet(specifier)
    except Exception:  # noqa: BLE001 - an unparsable spec should not block the report
        return True


def _codex_logged_in(env: dict[str, str]) -> bool:
    auth = Path(env.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    try:
        data = json.loads(auth.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("tokens") or data.get("OPENAI_API_KEY"))


def _litellm_credentials(name: str, model: str, env: dict[str, str]) -> CheckResult:
    try:
        import litellm

        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            report = litellm.validate_environment(model)
        finally:
            os.environ.clear()
            os.environ.update(saved)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, Status.WARN, f"could not work out the credentials for {model}: {exc}")
    missing = report.get("missing_keys") or []
    if missing:
        return CheckResult(name, Status.FAIL, f"{model} needs {', '.join(missing)}", f"export {missing[0]}=...")
    return CheckResult(name, Status.OK, f"credentials found for {model}")


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


# ----------------------------------------------------------------------- output

_ICONS = {Status.OK: "✅", Status.WARN: "⚠️ ", Status.FAIL: "❌", Status.SKIP: "➖"}
_STYLES = {Status.OK: "green", Status.WARN: "yellow", Status.FAIL: "red", Status.SKIP: "dim"}


def render(results: Iterable[CheckResult], options: DoctorOptions, console=None) -> None:
    from rich.console import Console
    from rich.table import Table

    console = console or Console()
    results = list(results)
    table = Table(
        title=(
            f"SREGym doctor — agent {options.agent}, model {options.model}, "
            f"profile {options.profile}, internet {options.internet_access}"
        ),
        show_lines=False,
        expand=False,
    )
    table.add_column("", width=2)
    table.add_column("Check", style="bold")
    table.add_column("Result")
    for r in results:
        detail = f"[{_STYLES[r.status]}]{r.detail}[/]"
        if r.fix and r.status in (Status.FAIL, Status.WARN):
            detail += f"\n[cyan]→ {r.fix}[/]"
        table.add_row(_ICONS[r.status], r.name, detail)
    console.print(table)

    fails = sum(r.status == Status.FAIL for r in results)
    warns = sum(r.status == Status.WARN for r in results)
    if fails:
        console.print(f"[red bold]{fails} blocker(s)[/], {warns} warning(s). Fix the blockers before starting a run.")
    elif warns:
        console.print(f"[green bold]Ready to run[/] with {warns} warning(s).")
    else:
        console.print("[green bold]Ready to run.[/]")


def run_doctor(options: DoctorOptions, console=None) -> int:
    """Run every check, print the report, and return a process exit code."""
    results = Doctor(options).run_all()
    render(results, options, console)
    return 1 if any(r.status == Status.FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(run_doctor(DoctorOptions()))
