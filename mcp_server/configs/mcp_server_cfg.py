from pydantic import BaseModel, Field


class McpServerCfg(BaseModel):
    """mcp server config"""

    mcp_server_port: int = Field(
        description="port number of mcp server",
        gt=0,
    )

    expose_server: bool = Field(description="If true, will use 0.0.0.0 for arg host otherwise use 127.0.0.0")
