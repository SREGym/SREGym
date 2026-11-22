"""Prepare recorded ATIF content for the trace reader, without interpreting it."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from atif_converter.atif.content import ContentPart
from atif_converter.atif.step import Step
from atif_converter.atif.trajectory import Trajectory

from .catalog import Catalog, sregym_metadata

PAGE_SIZE = 25
PREVIEW_SIZE = 8000
TRACE_FILTERS = ("step_page", "focus", "search", "role", "tool", "stage", "mode")
READER_FIELDS = ("run", "doc", "tab", *TRACE_FILTERS)


def pretty(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def query_url(query: Mapping[str, str], **changes: Any) -> str:
    values = {**query, **changes}
    return "/?" + urlencode({key: value for key, value in values.items() if value is not None and value != ""})


def positive_integer(value: str | int | None, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def content_text(value: str | list[ContentPart] | None) -> str:
    if isinstance(value, str):
        return value
    return "\n".join(part.text if part.type == "text" else f"[Image: {part.source.path}]" for part in (value or []))


def primary_argument(arguments: dict[str, Any]) -> dict[str, str] | None:
    for key, label in (
        ("cmd", "Command"),
        ("command", "Command"),
        ("code", "Code"),
        ("query", "Query"),
        ("url", "URL"),
        ("input", "Input"),
    ):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return {"label": label, "text": value, "preview": value.strip()[:90].splitlines()[0]}
    return None


def select_document(root: Trajectory, key: str) -> Trajectory:
    document = root
    try:
        parts = key.split(".") if key else []
        if len(parts) > 20:
            raise ValueError
        for part in parts:
            index = int(part)
            if index < 0:
                raise ValueError
            document = (document.subagent_trajectories or [])[index]
    except (ValueError, IndexError) as exc:
        raise ValueError("The subagent trajectory does not exist.") from exc
    return document


def field_value(step: Step, field: str) -> str | list[ContentPart] | None:
    try:
        if field == "message":
            return step.message
        if field == "reasoning":
            return step.reasoning_content
        if field == "metrics":
            return pretty(step.metrics.model_dump(exclude_none=True)) if step.metrics else None
        if field == "extra":
            return pretty(step.extra) if step.extra else None
        kind, _, suffix = field.partition("-")
        index = int(suffix)
        if index < 0:
            raise IndexError
        if kind == "arguments":
            return pretty((step.tool_calls or [])[index].arguments)
        if kind == "output":
            return step.observation.results[index].content if step.observation else None
    except (ValueError, IndexError) as exc:
        raise ValueError("The selected field does not exist.") from exc
    raise ValueError("The selected field does not exist.")


def stage_name(document: Trajectory, step_id: int) -> str:
    metadata = sregym_metadata(document)
    stages = metadata.get("stages")
    if isinstance(stages, list):
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            first, last = stage.get("first_step"), stage.get("last_step")
            if isinstance(first, int) and isinstance(last, int) and first <= step_id <= last:
                return str(stage.get("stage", "Unknown"))
    boundary = metadata.get("diagnosis_submitted_step")
    if isinstance(boundary, int):
        return "Diagnosis" if step_id <= boundary else "After diagnosis submission"
    return "Unknown"


class TraceReader:
    """Build one document's template context, retaining complete-content links."""

    def __init__(self, root: Trajectory, path: Path, catalog: Catalog, query: Mapping[str, str]):
        self.root = root
        self.path = path
        self.catalog = catalog
        self.query = query
        self.doc_key = query.get("doc", "")
        self.document = select_document(root, self.doc_key)

    def _link(self, **changes: Any) -> str:
        return query_url(self.query, **{**dict.fromkeys(TRACE_FILTERS), **changes})

    def _block(
        self, value: str | list[ContentPart] | None, step: Step, field: str, *, label: str | None = None
    ) -> dict:
        text = content_text(value)
        parameters = {**self.query, "doc": self.doc_key, "step": step.step_id, "field": field}
        image_parameters = {key: parameters[key] for key in ("run", "doc", "step", "field")}
        images = [
            {
                "path": part.source.path,
                "url": "/image?" + urlencode({**image_parameters, "part": index})
                if "://" not in part.source.path
                else None,
            }
            for index, part in enumerate(value if isinstance(value, list) else [])
            if part.type == "image"
        ]
        needle = self.query.get("search", "").casefold()
        return {
            "text": text[:PREVIEW_SIZE],
            "truncated": len(text) > PREVIEW_SIZE,
            "matched": bool(needle) and needle in text.casefold(),
            "label": label or field.split("-")[0].capitalize(),
            "url": "/text?" + urlencode(parameters),
            "images": images,
        }

    def _reference_url(self, trajectory_id: str | None, trajectory_path: str | None) -> str | None:
        if trajectory_id is not None:
            for index, child in enumerate(self.document.subagent_trajectories or []):
                if child.trajectory_id == trajectory_id:
                    return self._link(doc=f"{self.doc_key}.{index}".lstrip("."))
            for index, child in enumerate(self.root.subagent_trajectories or []):
                if child.trajectory_id == trajectory_id:
                    return self._link(doc=str(index))
        key = self.catalog.reference(self.path, trajectory_path) if trajectory_path else None
        return self._link(run=key, doc=None) if key else None

    def _matches(self, step: Step) -> bool:
        if self.query.get("mode") == "tools" and not step.tool_calls:
            return False
        if self.query.get("role") and self.query["role"] != step.source:
            return False
        if self.query.get("stage") and self.query["stage"] != stage_name(self.document, step.step_id):
            return False
        if self.query.get("tool") and self.query["tool"] not in {c.function_name for c in step.tool_calls or []}:
            return False
        needle = self.query.get("search", "").casefold()
        if not needle:
            return True
        haystack = content_text(step.message) + (step.reasoning_content or "")
        haystack += pretty([call.model_dump() for call in step.tool_calls or []])
        haystack += "\n".join(
            content_text(result.content) for result in (step.observation.results if step.observation else [])
        )
        return needle in haystack.casefold()

    def _render_step(self, step: Step) -> dict:
        results = step.observation.results if step.observation else []
        calls = []
        for index, call in enumerate(step.tool_calls or []):
            field = f"arguments-{index}"
            primary = primary_argument(call.arguments)
            if primary:
                primary = {
                    **self._block(primary["text"], step, field, label=primary["label"]),
                    "preview": primary["preview"],
                }
            calls.append(
                {
                    "call": call,
                    "primary": primary,
                    "arguments": self._block(pretty(call.arguments), step, field),
                    "outputs": [i for i, result in enumerate(results) if result.source_call_id == call.tool_call_id],
                }
            )
        outputs = [
            {
                "content": self._block(result.content, step, f"output-{index}"),
                "refs": [
                    {
                        "name": ref.trajectory_id or ref.trajectory_path,
                        "url": self._reference_url(ref.trajectory_id, ref.trajectory_path),
                    }
                    for ref in result.subagent_trajectory_ref or []
                ],
            }
            for index, result in enumerate(results)
        ]
        return {
            "step": step,
            "stage": stage_name(self.document, step.step_id),
            "message": self._block(step.message, step, "message"),
            "reasoning": self._block(step.reasoning_content, step, "reasoning"),
            "metrics": self._block(field_value(step, "metrics"), step, "metrics"),
            "extra": self._block(field_value(step, "extra"), step, "extra"),
            "calls": calls,
            "outputs": outputs,
            "unlinked": [i for i, result in enumerate(results) if result.source_call_id is None],
        }

    def context(self) -> dict:
        document = self.document
        steps = [step for step in document.steps if self._matches(step)]
        pages = max(1, (len(steps) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(positive_integer(self.query.get("step_page")), pages)
        if self.query.get("focus"):
            focus = positive_integer(self.query["focus"])
            position = next((i for i, step in enumerate(steps) if step.step_id == focus), None)
            if position is not None:
                page = position // PAGE_SIZE + 1
        continuation = document.continued_trajectory_ref
        continued_key = self.catalog.reference(self.path, continuation) if continuation else None
        return {
            "document": document,
            "children": [
                {
                    "name": child.agent.name,
                    "id": child.trajectory_id,
                    "url": self._link(doc=f"{self.doc_key}.{i}".lstrip(".")),
                }
                for i, child in enumerate(document.subagent_trajectories or [])
            ],
            "steps": [self._render_step(step) for step in steps[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]],
            "step_page": page,
            "step_pages": pages,
            "step_count": len(steps),
            "stage_options": sorted({stage_name(document, step.step_id) for step in document.steps}),
            "tool_options": sorted({call.function_name for step in document.steps for call in step.tool_calls or []}),
            "continued_url": self._link(run=continued_key, doc=None) if continued_key else None,
            "parent_url": self._link(doc=self.doc_key.rpartition(".")[0]),
            "metadata": pretty(document.model_dump(exclude={"steps", "subagent_trajectories"}, exclude_none=True))[
                :PREVIEW_SIZE
            ],
        }
