"""


Usage :
    python codex_to_trajectory.py <codex.txt> -o <output.jsonl> --problem-id <id>


"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Content-block helpers
# ---------------------------------------------------------------------------


def _text_from_content(content: Any) -> str:
    """Return a plain-text representation of a content value."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "output_text":
                    parts.append(block.get("text", ""))
                elif btype in ("image", "file"):
                    parts.append(f"[{btype}]")
                else:
                    # Fallback: dump the block
                    parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return str(content) if content is not None else ""


def _tool_calls_from_content(content: Any) -> list[dict]:
    """Extract tool_call dicts from content blocks (for assistant messages)."""
    calls: list[dict] = []
    if not isinstance(content, list):
        return calls
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype in ("tool_use", "function_call"):
            calls.append(
                {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "args": block.get("input", None) or block.get("arguments", {}),
                    "type": "tool_call",
                }
            )
    return calls


def _tool_results_from_content(content: Any) -> list[dict]:
    """Extract tool_result dicts from content blocks (for user/tool messages)."""
    results: list[dict] = []
    if not isinstance(content, list):
        return results
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype in ("tool_result", "function_call_output"):
            inner = block.get("content", block.get("output", ""))
            results.append(
                {
                    "role": "tool",
                    "tool_use_id": block.get("tool_use_id", block.get("call_id", "")),
                    "content": _text_from_content(inner),
                }
            )
    return results


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


def _parse_codex_json(input_path: Path) -> tuple[list[dict[str, Any]], bool]:
    """
    Parse a `codex exec --json` output file into a flat list of messages.

    Returns:
        (messages, submitted) where submitted=True when a success signal is found.
    """
    messages: list[dict[str, Any]] = []
    submitted = False
    pending_function_call: dict | None = None  # accumulates a streamed function call

    with input_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(event, dict):
                continue

            etype = event.get("type", "")

            # ------------------------------------------------------------------
            # A. Standard message event: {"type": "message", "role": "...", "content": ...}
            # ------------------------------------------------------------------
            if etype == "message":
                role = event.get("role", "")
                content = event.get("content", "")

                tool_results = _tool_results_from_content(content)
                if tool_results:
                    # Tool-result bearing user message
                    messages.extend(tool_results)
                    continue

                tool_calls = _tool_calls_from_content(content) if role == "assistant" else []
                text = _text_from_content(content)

                m: dict[str, Any] = {"role": role or "user", "content": text}
                if tool_calls:
                    m["tool_calls"] = tool_calls
                if text.strip() or tool_calls:
                    messages.append(m)

            # ------------------------------------------------------------------
            # B. Streamed function_call event (Codex sometimes streams these)
            # ------------------------------------------------------------------
            elif etype == "function_call":
                pending_function_call = {
                    "id": event.get("call_id", event.get("id", "")),
                    "name": event.get("name", ""),
                    "args": event.get("arguments", event.get("input", {})),
                    "type": "tool_call",
                }

            elif etype == "function_call_output":
                # Flush any pending function call first
                if pending_function_call:
                    # Attach the tool call to the previous assistant message or create one
                    if messages and messages[-1].get("role") == "assistant":
                        tc = messages[-1].setdefault("tool_calls", [])
                        tc.append(pending_function_call)
                    else:
                        messages.append(
                            {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [pending_function_call],
                            }
                        )
                    pending_function_call = None

                output = event.get("output", event.get("content", ""))
                messages.append(
                    {
                        "role": "tool",
                        "tool_use_id": event.get("call_id", event.get("tool_use_id", "")),
                        "content": _text_from_content(output),
                    }
                )

            # ------------------------------------------------------------------
            # C. Inline assistant turn (some Codex versions wrap differently)
            # ------------------------------------------------------------------
            elif etype in ("assistant", "user"):
                role = etype
                content = event.get("content", event.get("message", ""))
                tool_calls = _tool_calls_from_content(content) if role == "assistant" else []
                tool_results = _tool_results_from_content(content) if role == "user" else []

                if tool_results:
                    messages.extend(tool_results)
                else:
                    text = _text_from_content(content)
                    m = {"role": role, "content": text}
                    if tool_calls:
                        m["tool_calls"] = tool_calls
                    if text.strip() or tool_calls:
                        messages.append(m)

            # ------------------------------------------------------------------
            # D. Usage / completion signals
            # ------------------------------------------------------------------
            elif "usage" in event and etype in ("", "usage", "completion"):
                submitted = True

            # ------------------------------------------------------------------
            # E. Heuristic: top-level "role" without "type" wrapper
            # ------------------------------------------------------------------
            elif "role" in event and "content" in event:
                role = event["role"]
                content = event["content"]
                tool_calls = _tool_calls_from_content(content) if role == "assistant" else []
                tool_results = _tool_results_from_content(content) if role == "user" else []

                if tool_results:
                    messages.extend(tool_results)
                else:
                    text = _text_from_content(content)
                    m = {"role": role, "content": text}
                    if tool_calls:
                        m["tool_calls"] = tool_calls
                    if text.strip() or tool_calls:
                        messages.append(m)

    # Flush any trailing pending function call
    if pending_function_call:
        if messages and messages[-1].get("role") == "assistant":
            messages[-1].setdefault("tool_calls", []).append(pending_function_call)
        else:
            messages.append(
                {"role": "assistant", "content": "", "tool_calls": [pending_function_call]}
            )

    return messages, submitted


# ---------------------------------------------------------------------------
# Event builder (same pattern as claudecode_to_trajectory)
# ---------------------------------------------------------------------------


def _build_incremental_events(
    messages: list[dict[str, Any]],
    submitted: bool,
    stage: str,
    problem_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    """
    Emit one event record per agent step (tool-result boundary), each containing
    the cumulative message history up to that point.
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
            num_steps += 1
            # Collect consecutive tool messages
            while i + 1 < len(messages) and messages[i + 1].get("role") == "tool":
                i += 1
                accumulated.append(messages[i])
                num_steps += 1

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

    # Final event capturing the complete conversation
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def convert(
    input_path: Path,
    output_path: Path,
    problem_id: str,
    stage: str = "diagnosis",
) -> Path:
    """
    Convert a Codex JSON output file to a stratus JSONL trajectory.

    Args:
        input_path:  Path to the codex.txt output file.
        output_path: Destination JSONL file (created/overwritten).
        problem_id:  SREGym problem identifier.
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

    messages, submitted = _parse_codex_json(input_path)
    events = _build_incremental_events(messages, submitted, stage, problem_id, timestamp)

    with output_path.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "metadata",
                    "problem_id": problem_id,
                    "timestamp": timestamp,
                    "timestamp_readable": timestamp_readable,
                    "total_stages": 1,
                    "total_events": len(events),
                    "agent": "codex",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {"type": "stage_start", "stage": stage, "num_events": len(events)},
                ensure_ascii=False,
            )
            + "\n"
        )
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    print(f"[codex_to_trajectory] Wrote {len(events)} event(s) → {output_path}")
    return output_path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert Codex JSON output to stratus JSONL trajectory."
    )
    ap.add_argument("input", help="Path to codex.txt")
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
