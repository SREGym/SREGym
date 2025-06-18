from pydantic import BaseModel, Field, field_validator
from pathlib import Path
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

parent_parent_dir = Path(__file__).resolve().parent.parent
output_parent_dir = parent_parent_dir / "data"


class KubectlToolCfg(BaseModel):
    retry_wait_time: float = Field(
        default=60,
        description="Seconds to wait between retries.",
        gt=0
    )

    forbid_unsafe_commands: bool = Field(
        default=False,
        description="Forbid unsafe commands in the rollback tool.",
    )

    verify_dry_run: bool = Field(
        default=False,
        description="Enable verification of dry run results.",
    )

    # Update "default" with session id if using remote mcp server
    # If you see default dir, something went wrong.
    output_dir: str = Field(
        default=str(output_parent_dir / "default"),
        description="Directory to store some data used by kubectl server."
    )

    namespace: str = Field(
        default="",
        description="Kubernetes namespace to use for the agent.",
    )

    use_rollback_stack: bool = Field(
        default=True,
        description="Enable rollback stack for the rollback tool.",
    )

    """ Rollback Tool Configuration """
    validate_rollback: bool = Field(
        default=False,
        description="Enable generation of validation information",
    )

    clear_replicaset: bool = Field(
        default=True,
        description="Enable clearing of replicaset after rolling back deployment.",
    )  # Warning: This part may be harmful to the system. Use with caution.

    clear_rs_wait_time: float = Field(
        default=5,
        description="Seconds to wait before clearing replicaset.",
    )

    @field_validator("output_dir")
    @classmethod
    def validate_output_dir(cls, v):
        output_dir = v
        if not os.path.exists(output_dir):
            logger.info(f"creating output directory {v}")
            os.makedirs(output_dir, exist_ok=True)
        else:
            logger.info(f"Directory {v} exists already")

        if not os.access(output_dir, os.W_OK):
            raise PermissionError(f"Output directory {output_dir} is not writable.")
        return output_dir
