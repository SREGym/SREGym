import logging
from pathlib import Path

from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

parent_dir = Path(__file__).resolve().parent


class BaseAgentCfg(BaseModel):
    max_round: int = Field(description="maximum rounds allowed for tool calling", gt=0)

    prompts_file_path: str = Field(
        description="prompts used for diagnosis agent",
    )

    sync_tools: list[str] = Field(
        description="provided sync tools for the agent",
    )

    async_tools: list[str] = Field(
        description="provided async tools for the agent",
    )
