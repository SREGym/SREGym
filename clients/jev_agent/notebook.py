"""The notebook: everything the decision model sees about the investigation so far.

It holds the app info, the rendered overview, the observations (one per executed action), hypotheses from an
LLM escalation, and the set of actions already taken. `state(budget_tokens)` renders it for a decision request,
compressing in a fixed order when it does not fit:

1. observations the model judged normal (abnormal < 0.5) collapse to one line;
2. full text is kept only for the last FULL_TAIL observations and the abnormal ones (>= 0.5);
3. overview rows for healthy components are dropped;
4. the overview is re-rendered smaller (when a re-render callback is given), then full observations are cut
   to SHORT_LINES lines.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from clients.jev_agent.types import Observation, ToolResult
from clients.jev_diag.collector import estimate_tokens

logger = logging.getLogger("all.jev_agent.notebook")

# Tool lines that report history, not current state: stopped error signatures, start-up crashes that recovered,
# events from before a pod became Ready, events of pods that are gone. Names in them are not leads.
HISTORY_PREFIXES = (
    "stopped x",
    "start-up crash, since recovered",
    "start-up history",
    "events of pods no longer present",
)


# The logs tool's structured link: components that ongoing errors (or a crash's last words) name.
LEAD_PREFIX = "error lines name other components:"


def named_in(line: str) -> list[str]:
    """Component ids listed on a LEAD_PREFIX line, else []."""
    if not (line or "").startswith(LEAD_PREFIX):
        return []
    return [c.strip() for c in line[len(LEAD_PREFIX) :].split(",") if c.strip()]


def lead_lines(lines: list[str]) -> list[str]:
    """The lines whose mentions of other components can point the investigation somewhere."""
    return [ln for ln in lines or [] if ln and not ln.lstrip().startswith(HISTORY_PREFIXES)]


def mentions_id(cid: str, text: str) -> bool:
    """Whole-token match of a component id like deployment/frontend (not deployment/frontend-proxy)."""
    return re.search(rf"(?<![a-z0-9_./-]){re.escape(cid)}(?![a-z0-9_-])", text) is not None


FULL_TAIL = 6
SHORT_LINES = 3
ONE_LINE_CHARS = 180
_ID_KEYS = ("id", "cid", "component", "name")


def action_key(tool: str, target: str) -> str:
    """The menu key of a tool run on a target (`tool:target`)."""
    return f"{tool}:{target}"


def split_action(key: str) -> tuple[str, str]:
    """(tool, target) of a menu key."""
    tool, _, target = key.partition(":")
    return tool, target


def _row_id(row: Any) -> str | None:
    """The component id of an overview row (its first string id-like field), or None."""
    if isinstance(row, dict):
        for k in _ID_KEYS:
            if isinstance(row.get(k), str):
                return row[k]
    return None


def _compact(value: Any, limit: int = 80) -> str:
    """Short hint from an overview row: scalar fields only (the full row is in the state's overview)."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        parts = []
        for k, v in value.items():
            if k in _ID_KEYS or isinstance(v, bool) or v in (None, ""):
                continue
            if isinstance(v, str | int | float):
                parts.append(f"{k}={v}")
            elif isinstance(v, dict):
                sub = [
                    (kk, vv) for kk, vv in v.items() if isinstance(vv, str | int | float) and not isinstance(vv, bool)
                ]
                parts += [f"{k}.{kk}={vv}" for kk, vv in sub[:2]]
        text = "; ".join(parts)
    else:
        text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def overview_hints(overview: dict) -> dict[str, str]:
    """Short generic facts per component from the full overview (overview.build_overview output)."""
    out: dict[str, str] = {}
    for cid, c in ((overview or {}).get("components") or {}).items():
        if not isinstance(c, dict):
            continue
        parts = []
        rep = c.get("replicas") or {}
        if rep:
            parts.append(f"{rep.get('ready', 0)}/{rep.get('desired', 0)} ready")
        restarts = sum(int(p.get("restarts") or 0) for p in c.get("pods") or [] if isinstance(p, dict))
        if restarts:
            parts.append(f"{restarts} restarts")
        events = (c.get("warning_events") or {}).get("count") if isinstance(c.get("warning_events"), dict) else None
        if events:
            parts.append(f"{events} warning events")
        lines = (c.get("errors") or {}).get("ongoing_lines") if isinstance(c.get("errors"), dict) else None
        if lines:
            parts.append(f"{lines} ongoing error log lines")
        if c.get("alerts"):
            parts.append(f"{len(c['alerts'])} firing alerts")
        if c.get("recent_change") or c.get("template_changes") or c.get("modified_after_creation_s"):
            parts.append("recently changed")
        out[cid] = ", ".join(parts)
    return out


class Notebook:
    """The investigation's state: the observations made, the actions done, and how they are shown to the model."""

    def __init__(
        self,
        app_info: dict,
        overview: dict,
        *,
        scores: dict[str, float] | None = None,
        rerender: Callable[[int], dict] | None = None,
        hints: dict[str, str] | None = None,
    ) -> None:
        """An empty notebook for the application, with the rendered overview, component scores and per-target hints."""
        self.app_info = app_info or {}
        self.overview = overview or {}
        self.scores = dict(scores or {})
        self.rerender = rerender
        self.hints = dict(hints or {})  # target -> short known facts (see overview_hints)
        self.observations: list[Observation] = []
        self.hypotheses: list[dict] = []
        self.actions_done: set[str] = set()
        self.last_compression: list[str] = []
        self.error_labels: dict[str, str] = {}  # error line -> class the decision model gave it (ERROR_CLASSES)

    # ------------------------------------------------------------------ updates

    def add(self, step: int, tool: str, target: str, result: ToolResult) -> Observation:
        """Record a tool result as the next observation and mark the action done."""
        lines = [str(x) for x in (result.lines or [])]
        if result.error and not lines:
            lines = [f"tool error: {result.error}"]
        obs = Observation(
            id=f"O{len(self.observations) + 1}", step=step, tool=tool, target=target, lines=lines, error=result.error
        )
        self.observations.append(obs)
        self.actions_done.add(action_key(tool, target))
        return obs

    def last(self) -> Observation | None:
        """The most recent observation, or None."""
        return self.observations[-1] if self.observations else None

    def get(self, oid: str) -> Observation | None:
        """The observation with id `oid`, or None."""
        return next((o for o in self.observations if o.id == oid), None)

    def set_hypotheses(self, hypotheses: list[dict]) -> None:
        """Keep up to three hypotheses (from an escalation), numbered H1..H3."""
        self.hypotheses = [{"id": f"H{i + 1}", **h} for i, h in enumerate(hypotheses[:3])]

    # ------------------------------------------------------------------ rendering

    @staticmethod
    def one_line(o: Observation) -> str:
        """One line for an observation: its id, tool, target, first finding and the abnormality judgement."""
        first = o.lines[0] if o.lines else "(no findings)"
        if len(first) > ONE_LINE_CHARS:
            first = first[: ONE_LINE_CHARS - 3] + "..."
        more = f" [+{len(o.lines) - 1} more lines]" if len(o.lines) > 1 else ""
        judged = f" (abnormal p={o.abnormal:.2f})" if o.abnormal is not None else ""
        return f"{o.id} {o.tool} {o.target}: {first}{more}{judged}"

    @staticmethod
    def full(o: Observation, max_lines: int | None = None) -> dict:
        """An observation as a dict for the model, cut to `max_lines` lines when given."""
        lines = o.lines if max_lines is None else o.lines[:max_lines]
        out: dict[str, Any] = {"id": o.id, "step": o.step, "action": action_key(o.tool, o.target), "lines": lines}
        if max_lines is not None and len(o.lines) > max_lines:
            out["omitted_lines"] = len(o.lines) - max_lines
        if o.error:
            out["error"] = o.error
        if o.abnormal is not None:
            out["abnormal_p"] = round(o.abnormal, 2)
        return out

    def overview_row(self, target: str) -> Any:
        """The overview row of a target, whatever shape the overview's component list has."""
        comps = self.overview.get("components") if isinstance(self.overview, dict) else None
        if isinstance(comps, dict):
            return comps.get(target)
        if isinstance(comps, list):
            for row in comps:
                if _row_id(row) == target or (isinstance(row, str) and target in row):
                    return row
        return None

    def known(self, target: str) -> str:
        """What is already known about a target, for option descriptions."""
        done = [o for o in self.observations if o.target == target]
        if done:
            parts = []
            for o in done:
                flag = ", abnormal" if (o.abnormal or 0) >= 0.5 else (", normal" if o.abnormal is not None else "")
                parts.append(f"{o.tool} ({o.id}{flag})")
            text = "examined: " + ", ".join(parts)
        else:
            text = "not yet examined"
        # observations about other targets that name this one (e.g. error lines naming a dependency)
        short = target.split("->")[-1].strip()
        mentioned = [
            o.id for o in self.observations if o.target != target and any(short in named_in(ln) for ln in o.lines)
        ]
        if mentioned:
            text += f"; named in {', '.join(mentioned[-3:])}"
        hint = self.hints.get(target)
        if hint is None:
            row = self.overview_row(target)
            hint = _compact(row) if row is not None else ""
        if hint:
            text += f"; {hint}"
        return text

    def _app(self) -> dict:
        """The application's name, namespaces and description (cut to 800 characters) as shown in the state."""
        info = self.app_info
        desc = str(info.get("descriptions") or info.get("description") or "")
        return {
            "name": info.get("app_name") or info.get("name"),
            "namespaces": info.get("namespaces") or [info.get("namespace")],
            "description": desc[:800],
        }

    def _drop_healthy(self, overview: dict) -> dict:
        """A copy of the overview without the components marked healthy or scoring 0 or less, with how many were left
        out."""
        overview = copy.deepcopy(overview)
        comps = overview.get("components") if isinstance(overview, dict) else None

        def healthy(cid: str | None, row: Any) -> bool:
            if isinstance(row, dict) and row.get("healthy") is True:
                return True
            return cid is not None and cid in self.scores and self.scores[cid] <= 0

        if isinstance(comps, dict):
            kept = {cid: row for cid, row in comps.items() if not healthy(cid, row)}
            overview["components"] = kept
            overview["healthy_components_omitted"] = len(comps) - len(kept)
        elif isinstance(comps, list):
            kept = []
            for row in comps:
                cid = _row_id(row)
                if cid is None and isinstance(row, str):
                    cid = next((c for c in self.scores if c in row), None)
                if not healthy(cid, row):
                    kept.append(row)
            overview["components"] = kept
            overview["healthy_components_omitted"] = len(comps) - len(kept)
        return overview

    def state(self, budget_tokens: int) -> dict:
        """The state shown to the model, shortened step by step until it fits `budget_tokens`."""
        applied: list[str] = []
        obs = self.observations
        n = len(obs)
        mode = {o.id: "full" for o in obs}
        overview = self.overview
        max_lines: int | None = None
        hypotheses = self.hypotheses

        def build() -> dict:
            """The state with each observation rendered in its current mode (full or one line)."""
            rendered = []
            for o in obs:
                if mode[o.id] == "full":
                    rendered.append(self.full(o, max_lines))
                else:
                    rendered.append(self.one_line(o))
            st: dict[str, Any] = {"app": self._app(), "overview": overview, "observations": rendered}
            if hypotheses:
                st["hypotheses"] = hypotheses
            return st

        state = build()
        if estimate_tokens(state) <= budget_tokens:
            self.last_compression = applied
            return state

        # 1. collapse observations judged normal
        for o in obs:
            if o.abnormal is not None and o.abnormal < 0.5:
                mode[o.id] = "line"
        applied.append("collapse_normal_observations")
        state = build()
        if estimate_tokens(state) > budget_tokens:
            # 2. full text only for the last FULL_TAIL and the abnormal ones
            for i, o in enumerate(obs):
                if i < n - FULL_TAIL and not (o.abnormal is not None and o.abnormal >= 0.5):
                    mode[o.id] = "line"
            applied.append("collapse_older_observations")
            state = build()
        if estimate_tokens(state) > budget_tokens:
            # 3. drop healthy rows from the overview
            overview = self._drop_healthy(overview)
            applied.append("drop_healthy_overview_rows")
            state = build()
        if estimate_tokens(state) > budget_tokens and self.rerender is not None:
            try:
                overview = self._drop_healthy(self.rerender(max(500, budget_tokens // 4)))
                applied.append("rerender_overview_smaller")
                state = build()
            except Exception as exc:  # noqa: BLE001 - keep the dropped version
                logger.debug("smaller overview rendering failed: %s", exc)
        if estimate_tokens(state) > budget_tokens:
            max_lines = SHORT_LINES
            applied.append(f"cut_full_observations_to_{SHORT_LINES}_lines")
            state = build()
        state["compression"] = applied
        self.last_compression = applied
        return state
