import os
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

REGISTRY_ENV = "SREGYM_AGENT_REGISTRY"
DEFAULT_REG_PATH = Path(os.environ.get(REGISTRY_ENV, "agents.yaml"))


def registry_path(repo_root: Path) -> Path:
    """The agent registry a run reads: $SREGYM_AGENT_REGISTRY when set, else repo_root/agents.yaml.

    Read when called, not at import, so a wrapper that sets the variable before calling into
    SREGym is honoured. The file replaces agents.yaml rather than adding to it, so it must list
    every agent the run uses, including a CLI judge backend.

    A set override must name an existing file. Without this check list_agents would create an
    empty registry at a mistyped path and the run would fail later as "agent not found".
    """
    override = os.environ.get(REGISTRY_ENV, "").strip()
    if not override:
        return repo_root / "agents.yaml"
    path = Path(override).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"{REGISTRY_ENV} is set to {override!r}, but no file exists there")
    return path


@dataclass
class AgentRegistration:
    name: str
    kickoff_command: str | None = None
    kickoff_workdir: str | None = None
    kickoff_env: dict[str, str] | None = None
    install_script: str | None = None
    agent_version: str | None = None
    container_isolation: bool = True
    # False: the agent container gets no kubeconfig and no egress to the API proxy or to SREGym's
    # MCP server (its /kubectl tools), so its only route to the cluster is whatever MCP server it
    # is given (e.g. Radar). Default unchanged.
    cluster_access: bool = True
    # "module:attribute" of an object with before_fault_injection and/or
    # after_fault_injection; see sregym.conductor.hooks. Imported by main.py.
    hooks: str | None = None


def _ensure_file(path: Path):
    if not path.exists():
        path.write_text(yaml.safe_dump({"agents": []}, sort_keys=False))


def list_agents(path: Path = DEFAULT_REG_PATH) -> dict[str, AgentRegistration]:
    _ensure_file(path)
    data = yaml.safe_load(path.read_text()) or {"agents": []}
    out: dict[str, AgentRegistration] = {}
    for a in data.get("agents", []):
        out[a["name"]] = AgentRegistration(
            name=a["name"],
            kickoff_command=a.get("kickoff_command"),
            kickoff_workdir=a.get("kickoff_workdir"),
            kickoff_env=a.get("kickoff_env") or {},
            install_script=a.get("install_script"),
            agent_version=a.get("agent_version"),
            container_isolation=a.get("container_isolation", True),
            cluster_access=a.get("cluster_access", True),
            hooks=a.get("hooks"),
        )
    return out


def get_agent(name: str, path: Path = DEFAULT_REG_PATH) -> AgentRegistration | None:
    return list_agents(path).get(name)


def save_agent(reg: AgentRegistration, path: Path = DEFAULT_REG_PATH) -> None:
    _ensure_file(path)
    data = yaml.safe_load(path.read_text()) or {"agents": []}
    agents = [x for x in data.get("agents", []) if x.get("name") != reg.name]
    agents.append(asdict(reg))
    data["agents"] = agents
    path.write_text(yaml.safe_dump(data, sort_keys=False))
