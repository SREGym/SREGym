from pydantic import BaseModel, Field


class KubectlSessionCfg(BaseModel):
    """ kubectl tool session config"""
    session_cache_size: int = Field(
        default=10000,
        description="Max size of the session cache",
        gt=100,
    )

    session_ttl: int = Field(
        default=10 * 60,
        description="Time to live after last time session access (in seconds)",
        gt=30,
    )
