"""

Usage:
    python claudecode_to_trajectory.py <claude-code.txt> -o <output.jsonl> --problem-id <id>

"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def _extract_text(content: list[dict]) -> str:
    """Join all text blocks from a content list."""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _parse_stream_json(input_path: Path) -> tuple[list[dict[str, Any]], bool]:
    """
    Parse a claude --output-format stream-json file into a flat list of messages
    suitable for the stratus trajectory format.

    Returns:
        (messages, submitted) where submitted=True if the run ended successfully.

    Message format (API-style, supported by the visualizer's render_messages):
        {"role": "user"|"assistant"|"tool"|"system", "content": str, ...}
    """
    messages: list[dict[str, Any]] = []
    submitted = False

    with input_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            if etype == "system" and event.get("subtype") == "init":
                # System initialisation — emit a system message with the model info
                model = event.get("model", "")
                cwd = event.get("cwd", "")
                if model or cwd:
                    messages.append({"role": "system", "content": f"model={model} cwd={cwd}"})

            elif etype in ("user", "assistant"):
                msg = event.get("message") or {}
                role = msg.get("role", etype)
                content = msg.get("content", "")

                if isinstance(content, str):
                    if content.strip():
                        messages.append({"role": role, "content": content})
                    continue

                if not isinstance(content, list):
                    continue

                # Split content blocks by kind
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                tool_results: list[dict] = []

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        if text.strip():
                            text_parts.append(text)
                    elif btype == "tool_use":
                        tool_calls.append(
                            {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "args": block.get("input", {}),
                                "type": "tool_call",
                            }
                        )
                    elif btype == "tool_result":
                        tool_results.append(block)

                if tool_results:
                    # User message carrying tool results → one "tool" message per result
                    for tr in tool_results:
                        tr_content = tr.get("content", "")
                        if isinstance(tr_content, list):
                            tr_content = "\n".join(
                                b.get("text", "") for b in tr_content if isinstance(b, dict)
                            )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_use_id": tr.get("tool_use_id", ""),
                                "content": tr_content,
                            }
                        )
                else:
                    # Regular user or assistant turn
                    m: dict[str, Any] = {
                        "role": role,
                        "content": "\n".join(text_parts),
                    }
                    if tool_calls:
                        m["tool_calls"] = tool_calls
                    messages.append(m)

            elif etype == "result":
                submitted = event.get("subtype") == "success"

    return messages, submitted


def _build_incremental_events(
    messages: list[dict[str, Any]],
    submitted: bool,
    stage: str,
    problem_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    """
    Build a list of event records where each event contains the full cumulative
    message history up to that assistant turn.

    The visualizer picks the highest event_index per stage, so the final event
    contains the complete conversation.
    """
    events: list[dict[str, Any]] = []
    event_index = 0
    num_steps = 0
    accumulated: list[dict] = []

    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")
        accumulated.append(msg)

        if role == "tool":
            # Collect consecutive tool messages belonging to the same assistant turn
            num_steps += 1
            while i + 1 < len(messages) and messages[i + 1].get("role") == "tool":
                i += 1
                accumulated.append(messages[i])
                num_steps += 1

            # Emit an event after each batch of tool results (= end of one agent step)
            is_last = i == len(messages) - 1
            events.append(
                {
                    "type": "event",
                    "stage": stage,
                    "event_index": event_index,
                    "num_steps": num_steps,
                    "submitted": submitted if is_last else False,
                    "rollback_stack": "",
                    "messages": list(accumulated),
                    "last_message": accumulated[-1],
                    "problem_id": problem_id,
                    "timestamp": timestamp,
                }
            )
            event_index += 1

        i += 1

    # Always emit a final event even if the last message wasn't a tool result
    if not events or accumulated != events[-1]["messages"]:
        events.append(
            {
                "type": "event",
                "stage": stage,
                "event_index": event_index,
                "num_steps": num_steps,
                "submitted": submitted,
                "rollback_stack": "",
                "messages": list(accumulated),
                "last_message": accumulated[-1] if accumulated else {},
                "problem_id": problem_id,
                "timestamp": timestamp,
            }
        )

    return events


def convert(
    input_path: Path,
    output_path: Path,
    problem_id: str,
    stage: str = "diagnosis",
) -> Path:
    """
    Convert a Claude Code stream-json output file to a stratus JSONL trajectory.

    Args:
        input_path:  Path to the claude-code.txt output file.
        output_path: Destination JSONL file (created/overwritten).
        problem_id:  SREGym problem identifier (e.g. "readiness_probe_misconfiguration_hotel_reservation").
        stage:       Stage label to use for all events (default: "diagnosis").

    Returns:
        The output_path.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    timestamp = now.strftime("%m%d_%H%M")
    timestamp_readable = now.strftime("%Y-%m-%d %H:%M:%S")

    messages, submitted = _parse_stream_json(input_path)
    events = _build_incremental_events(messages, submitted, stage, problem_id, timestamp)

    with output_path.open("w", encoding="utf-8") as f:
        # Metadata line
        f.write(
            json.dumps(
                {
                    "type": "metadata",
                    "problem_id": problem_id,
                    "timestamp": timestamp,
                    "timestamp_readable": timestamp_readable,
                    "total_stages": 1,
                    "total_events": len(events),
                    "agent": "claudecode",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        # Stage marker
        f.write(
            json.dumps(
                {"type": "stage_start", "stage": stage, "num_events": len(events)},
                ensure_ascii=False,
            )
            + "\n"
        )
        # Events
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    print(f"[claudecode_to_trajectory] Wrote {len(events)} event(s) → {output_path}")
    return output_path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert Claude Code stream-json output to stratus JSONL trajectory."
    )
    ap.add_argument("input", help="Path to claude-code.txt")
    ap.add_argument("-o", "--output", required=True, help="Output .jsonl path")
    ap.add_argument("--problem-id", default="unknown", help="SREGym problem ID")
    ap.add_argument("--stage", default="diagnosis", help="Stage label (default: diagnosis)")
    args = ap.parse_args()

    convert(
        input_path=Path(args.input),
        output_path=Path(args.output),
        problem_id=args.problem_id,
        stage=args.stage,
    )


if __name__ == "__main__":
    main()
