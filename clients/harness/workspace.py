"""Append an optional source-workspace hint to agent instructions."""


def append_workspace_hint(instruction: str, app_info: dict) -> str:
    hint = (app_info.get("workspace_hint") or "").strip()
    if not hint:
        return instruction
    return f"{instruction.rstrip()}\n\n{hint}\n"
