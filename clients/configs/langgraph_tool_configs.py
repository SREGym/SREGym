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


langToolCfg = LangToolCfg(
    mcp_prometheus="http://127.0.0.1:8000/prometheus/sse", mcp_observability="http://127.0.0.1:8000/jaeger/sse"
)
