"""Experimental evidence-only control: same collection, no model ranking."""

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from clients.jev.observations import collect_observations


def create_server(log_path: Path, collector=collect_observations):
    server = FastMCP("observations")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def jev_observe(namespace: str) -> dict:
        """Collect current Pod status and recent logs without model advice.

        Uses the existing Kubernetes connection and the same bounded collection
        as the advice tool. Inspect these observations yourself. No model selects
        a cause or ranks these sources. Absence of errors does not prove recovery.
        """
        try:
            observations = await collector(namespace)
        except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired):
            return {
                "error": "observations_unavailable",
                "message": "Could not collect bounded observations. Use your normal read-only diagnostic tools.",
            }
        result = {
            "observations": [item.model_dump() for item in observations],
            "next_step": "Inspect the raw evidence yourself. Reproduce a current failed application operation and map its exact dependencies. Keep healthy controls and contrary evidence. No ranking or diagnosis has been supplied.",
        }
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "tool": "jev_observe",
                        "condition": "evidence_only",
                        "result": result,
                    }
                )
                + "\n"
            )
        return result

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-path", type=Path, required=True)
    args = parser.parse_args()
    args.log_path.parent.mkdir(parents=True, exist_ok=True)
    create_server(args.log_path).run(transport="stdio")
