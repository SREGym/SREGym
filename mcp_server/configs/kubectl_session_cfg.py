from pydantic import BaseModel, Field


class KubectlSessionCfg(BaseModel):
    """kubectl tool session config"""

    session_cache_size: int = Field(
        description="Max size of the session cache",
        gt=100,
    )

    session_ttl: int = Field(
        description="Time to live after last time session access (in seconds)",
        gt=30,
    )
