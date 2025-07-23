import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class LangToolCfg(BaseModel):
    mcp_prometheus: str = Field(description="url for prometheus mcp server")

    mcp_observability: str = Field(description="url for observability mcp server")

    min_len_to_sum: int = Field(
        description="Minimum length of text that will be summarized "
                    "first before being input to the main agent.",
        default=200,
        ge=50
    )

    use_summaries: bool = Field(
        description="Whether or not using summaries for too long texts.",
        default=True
    )


load_dotenv()

langToolCfg = LangToolCfg(
    mcp_prometheus=f"{os.environ['MCP_SERVER_URL']}/prometheus/sse",
    mcp_observability=f"{os.environ['MCP_SERVER_URL']}/jaeger/sse",
    mcp_kubectl=f"{os.environ['MCP_SERVER_URL']}/kubectl_mcp_tools/sse",
)
