import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ModelsConfig(BaseModel):
    agent: str = Field(default="gpt-4o")
    judge: str = Field(default="gpt-5")


class ServerConfig(BaseModel):
    api_hostname: str = "0.0.0.0"
    api_port: int = 8000
    mcp_server_port: int = 9954
    expose_server: bool = False
    session_cache_size: int = 10000
    session_ttl: int = 600


class LLMConfig(BaseModel):
    max_retries: int = 5
    init_retry_delay: int = 1


class ClusterConfig(BaseModel):
    wait_for_pod_ready_timeout: int = 600


class SREGymConfig(BaseModel):
    agent: str = Field(default="stratus")
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    n_attempts: int = Field(default=1, description="Number of attempts to run each problem")
    agent_timeout: int = Field(default=1800, description="Agent timeout in seconds after deployment")
    force_build: bool = Field(default=False, description="Force rebuild the agent Docker image")
    noise_config: str | None = Field(default=None, description="Path to noise configuration YAML file")


_config: SREGymConfig | None = None


def load_sregym_config(path: str | Path | None = None) -> SREGymConfig:
    global _config

    if path is None:
        path = Path(os.path.dirname(os.path.abspath(__file__))).parent / "sregym_config.yaml"

    path = Path(path)
    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        _config = SREGymConfig(**raw)
    else:
        _config = SREGymConfig()

    return _config


def get_sregym_config() -> SREGymConfig:
    global _config
    if _config is None:
        _config = load_sregym_config()
    return _config
