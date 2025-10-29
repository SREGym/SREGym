from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional
import os, yaml

DEFAULT_REG_PATH = Path(os.environ.get("SREGYM_AGENT_REGISTRY", "agents.yaml"))

@dataclass
class AgentRegistration:
    name: str
    kickoff_command: str | None = None
    kickoff_workdir: str | None = None
    kickoff_env: Dict[str, str] | None = None

def _ensure_file(path: Path):
    if not path.exists():
        path.write_text(yaml.safe_dump({"agents": []}, sort_keys=False))

def list_agents(path: Path = DEFAULT_REG_PATH) -> Dict[str, AgentRegistration]:
    _ensure_file(path)
    data = yaml.safe_load(path.read_text()) or {"agents": []}
    out: Dict[str, AgentRegistration] = {}
    for a in data.get("agents", []):
        out[a["name"]] = AgentRegistration(
            name=a["name"],
            kickoff_command=a.get("kickoff_command"),
            kickoff_workdir=a.get("kickoff_workdir"),
            kickoff_env=a.get("kickoff_env") or {},
        )
    return out

def get_agent(name: str, path: Path = DEFAULT_REG_PATH) -> Optional[AgentRegistration]:
    return list_agents(path).get(name)

def save_agent(reg: AgentRegistration, path: Path = DEFAULT_REG_PATH) -> None:
    _ensure_file(path)
    data = yaml.safe_load(path.read_text()) or {"agents": []}
    agents = [x for x in data.get("agents", []) if x.get("name") != reg.name]
    agents.append(asdict(reg))
    data["agents"] = agents
    path.write_text(yaml.safe_dump(data, sort_keys=False))
