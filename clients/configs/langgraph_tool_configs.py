from pydantic import BaseModel, Field


class LangToolCfg(BaseModel):
    mcp_prometheus: str = Field(description="url for prometheus mcp server")

    mcp_observability: str = Field(description="url for observability mcp server")


langToolCfg = LangToolCfg(
    mcp_prometheus="http://127.0.0.1:8000/prometheus/sse", mcp_observability="http://127.0.0.1:8000/jaeger/sse"
)
