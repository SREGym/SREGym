"""Shared validity of explicitly classified verification failures.

Older CSV rows without a class retain their existing interpretation. An explicit
non-agent failure never satisfies a difficulty sample or a resumed attempt.
"""

from collections.abc import Mapping


def non_agent_failure_classes(row: Mapping[str, object]) -> dict[str, str]:
    invalid = {}
    for stage in ("Diagnosis", "Mitigation"):
        success = str(row.get(stage + ".success", "")).strip().lower()
        classification = str(row.get(stage + ".failure_class", "") or "").strip().lower()
        if success not in {"true", "1", "yes"} and classification and classification != "agent_error":
            invalid[stage] = classification
    return invalid
