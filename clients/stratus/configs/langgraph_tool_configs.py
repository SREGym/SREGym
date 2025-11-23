import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()


# FIXME: name of class is misleading for now
class LanggraphToolConfig(BaseModel):
    prometheus_mcp_url: str = Field(
        description="url for prometheus mcp server",
        default=f"http://localhost:{os.getenv('MCP_SERVER_PORT', '9954')}/prometheus/sse",
    )
    jaeger_mcp_url: str = Field(
        description="url for jaeger mcp server",
        default=f"http://localhost:{os.getenv('MCP_SERVER_PORT', '9954')}/jaeger/sse",
    )
    kubectl_mcp_url: str = Field(
        description="url for kubectl mcp server",
        default=f"http://localhost:{os.getenv('MCP_SERVER_PORT', '9954')}/kubectl_mcp_tools/sse",
    )
    submit_mcp_url: str = Field(
        description="url for submit mcp server",
        default=f"http://localhost:{os.getenv('MCP_SERVER_PORT', '9954')}/submit/sse",
    )
    benchmark_submit_url: str = Field(
        description="url for the submission result destination, default to http://localhost:8000/submit",
        default=f"http://localhost:{os.getenv("API_PORT", "8000")}/submit",
    )
    benchmark_app_info_url: str = Field(
        description="url for getting benchmark application information, default to http://localhost:8000/get_app",
        default=f"http://localhost:{os.getenv("API_PORT", "8000")}/get_app",
    )
    benchmark_current_problem: str = Field(
        description="url for getting current benchmark problem, default to http://localhost:8000/get_problem",
        default=f"http://localhost:{os.getenv("API_PORT", "8000")}/get_problem",
    )

    min_len_to_sum: int = Field(
        description="Minimum length of text that will be summarized " "first before being input to the main agent.",
        default=200,
        ge=50,
    )

    use_summaries: bool = Field(description="Whether or not using summaries for too long texts.", default=True)
