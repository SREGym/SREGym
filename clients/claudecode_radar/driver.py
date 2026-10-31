"""Driver for Claude Code with the Radar MCP server.

Reuses clients.claudecode.driver (conductor handshake, submission, results),
swaps in RadarClaudeCodeAgent, and changes the task instruction's kubectl tool
line to name Radar's MCP tools. The preflight also
checks that Radar's MCP endpoint answers from inside the agent container, so a
run cannot start with the tools silently missing.
"""

import json
import sys

import requests

import clients.claudecode.driver as base
from clients.claudecode_radar.claudecode_agent import RadarClaudeCodeAgent, radar_mcp_url

base.ClaudeCodeAgent = RadarClaudeCodeAgent

# The task instruction's tool line names Radar's MCP tools instead of kubectl: the container has no
# kubectl, so upstream's line would be false.
# Upstream's text (clients/claudecode/driver.py) is not edited; only this client's copy changes.
KUBECTL_TOOL_LINE = "- You have access to kubectl commands to inspect and modify resources in namespace(s): "
RADAR_TOOL_LINE = (
    "- You have access to Radar's MCP tools (mcp__radar__*) to inspect and modify resources in namespace(s): "
)
_upstream_build_instruction = base.build_instruction


def build_instruction(app_info: dict) -> str:
    instruction = _upstream_build_instruction(app_info)
    if instruction.count(KUBECTL_TOOL_LINE) != 1:
        raise RuntimeError("upstream's kubectl tool line not found once in the task instruction")
    return instruction.replace(KUBECTL_TOOL_LINE, RADAR_TOOL_LINE)


base.build_instruction = build_instruction


def _check_radar_mcp() -> None:
    url = radar_mcp_url()
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "sregym-preflight", "version": "1"},
        },
    }
    try:
        r = requests.post(
            url,
            json=body,
            headers={"Accept": "application/json, text/event-stream"},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"Radar MCP preflight: cannot reach {url}: {e}")
        sys.exit(1)
    if r.status_code != 200:
        print(f"Radar MCP preflight: {url} returned HTTP {r.status_code}: {r.text[:300]}")
        sys.exit(1)
    payload = r.text
    if payload.startswith("event:") or "data:" in payload:
        payload = next(line[5:].strip() for line in payload.splitlines() if line.startswith("data:"))
    message = json.loads(payload)
    info = (message.get("result") or {}).get("serverInfo")
    if not info:
        print(f"Radar MCP preflight: {url} did not return an initialize result: {payload[:300]}")
        sys.exit(1)
    print(f"Radar MCP preflight OK: {info.get('name')} {info.get('version')} at {url}")


def run_preflight() -> None:
    _check_radar_mcp()
    base.run_preflight()


if __name__ == "__main__":
    base.main()
