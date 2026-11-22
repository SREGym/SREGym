"""Compact campaign and fault projections for browsing many recorded attempts."""

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime

from .catalog import Run


def filter_runs(runs: list[Run], query: Mapping[str, str]) -> list[Run]:
    needle = query.get("q", "").casefold()
    return [
        run
        for run in runs
        if needle in " ".join((run.name, run.key, run.agent, run.model, run.application)).casefold()
        and (not query.get("fault") or run.name == query["fault"])
        and all(
            not query.get(field) or getattr(run, field) == query[field]
            for field in ("campaign", "agent", "model", "status", "diagnosis", "mitigation")
        )
    ]


def grade_counts(runs: list[Run], field: str) -> dict[str, int]:
    return {grade: sum(getattr(run, field) == grade for run in runs) for grade in ("Pass", "Fail", "Unknown")}


def attempt_order(run: Run) -> tuple:
    return (0, int(run.attempt), run.key) if run.attempt.isdecimal() else (1, 0, run.key)


def recorded_time(run: Run) -> float:
    try:
        value = datetime.fromisoformat(run.started_at.replace("Z", "+00:00"))
        return value.timestamp() if value.tzinfo else 0
    except (ValueError, OverflowError):
        return 0


def campaign_groups(runs: list[Run], sort: str = "recent") -> list[dict]:
    groups: dict[str, list[Run]] = defaultdict(list)
    for run in runs:
        groups[run.campaign].append(run)
    rows = [
        {
            "key": key,
            "name": "Current directory" if key == "." else key,
            "runs": members,
            "agents": sorted({r.agent for r in members}),
            "models": sorted({r.model for r in members}),
            "faults": len({r.name for r in members}),
            "diagnosis": grade_counts(members, "diagnosis"),
            "mitigation": grade_counts(members, "mitigation"),
            "incomplete": sum(r.status == "incomplete" for r in members),
            "recorded_time": max(recorded_time(r) for r in members),
        }
        for key, members in groups.items()
    ]
    return sorted(
        rows,
        key=lambda row: (
            -row["mitigation"]["Fail"] if sort == "failures" else 0,
            -row["recorded_time"] if sort != "name" else 0,
            row["key"],
        ),
    )


def fault_groups(runs: list[Run], sort: str = "name") -> list[dict]:
    # Keep models and agents separate even when they share a campaign directory.
    groups: dict[tuple[str, str, str], list[Run]] = defaultdict(list)
    for run in runs:
        groups[run.name, run.agent, run.model].append(run)
    rows = [
        {
            "name": name,
            "agent": agent,
            "model": model,
            "application": next((r.application for r in members if r.application), ""),
            "runs": sorted(members, key=attempt_order),
            "diagnosis": grade_counts(members, "diagnosis"),
            "mitigation": grade_counts(members, "mitigation"),
            "incomplete": sum(r.status == "incomplete" for r in members),
        }
        for (name, agent, model), members in groups.items()
    ]
    return sorted(
        rows,
        key=lambda row: (
            -row["mitigation"]["Fail"] if sort == "failures" else 0,
            row["name"],
            row["agent"],
            row["model"],
        ),
    )
