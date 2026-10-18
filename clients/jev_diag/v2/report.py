"""The submitted diagnosis: origin, mechanism with concrete values, scope.

Observed facts and inferred links stay separate, and only components with observed symptoms are named as
affected, never everything reachable from the origin.
"""

from __future__ import annotations

from clients.jev_diag.v2.explain import PARTIAL, Explanation


def describe_object(object_id: str) -> str:
    """'kind/namespace/name' -> "Kind `name` in namespace `namespace`"; cluster-scoped 'kind/name' -> "Kind `name`"."""
    parts = object_id.split("/")
    if len(parts) == 3 and parts[0] == "interaction" and "--" in parts[2]:
        caller, callee = parts[2].split("--", 1)
        return f"the interaction between `{caller}` (caller) and `{callee}` (callee) in namespace `{parts[1]}`"
    if len(parts) == 3 and parts[0] == "input":
        return f"input source `{parts[2]}` in namespace `{parts[1]}`"
    if len(parts) == 3:
        return f"{parts[0]} `{parts[2]}` in namespace `{parts[1]}`"
    if len(parts) == 2:
        return f"{parts[0]} `{parts[1]}`"
    return f"`{object_id}`"


def build_report(causes: list[Explanation], *, observed: list[str], uncertain: bool = False) -> str:
    """Text for one or two independent causes; `observed` lists the evidence lines worth quoting."""
    lines: list[str] = []
    for n, cause in enumerate(causes, start=1):
        prefix = f"Cause {n}: " if len(causes) > 1 else ""
        lines.append(f"{prefix}Root cause: {describe_object(cause.origin)}.")
        if cause.pattern == PARTIAL:
            lines.append("Mechanism: not established from the collected evidence.")
        else:
            lines.append(f"Mechanism: {cause.mechanism}")
        if cause.values:
            lines.append("Concrete details: " + "; ".join(cause.values) + ".")
        if cause.contributing:
            lines.append("Contributing: " + ", ".join(describe_object(c) for c in cause.contributing) + ".")
        if cause.impact:
            lines.append("Observed impact: " + ", ".join(describe_object(c) for c in cause.impact) + ".")
    if observed:
        lines.append("Evidence:")
        lines += [f"- {line}" for line in observed]
    if uncertain:
        lines.append("Confidence: low; the investigation budget ended before the leading explanation was confirmed.")
    return "\n".join(lines)
