"""Read-only HTTP routes for browsing and inspecting local ATIF trajectories."""

from functools import partial
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from atif_converter.atif.content import ContentPart
from atif_converter.atif.trajectory import Trajectory

from .browse import attempt_order, campaign_groups, fault_groups, filter_runs, grade_counts
from .catalog import Catalog, Record, Run
from .reader import (
    PAGE_SIZE,
    PREVIEW_SIZE,
    READER_FIELDS,
    TRACE_FILTERS,
    TraceReader,
    content_text,
    field_value,
    positive_integer,
    pretty,
    query_url,
    select_document,
)

ASSETS = Path(__file__).parent


def create_app(source: Path) -> FastAPI:
    catalog = Catalog(source)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.catalog = catalog
    templates = Jinja2Templates(directory=ASSETS / "templates")
    templates.env.filters["number"] = lambda n: f"{n:,}" if isinstance(n, (int, float)) else "Unknown"
    app.mount("/static", StaticFiles(directory=ASSETS / "static"), name="static")

    @app.middleware("http")
    async def read_only_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; img-src 'self'; script-src 'none'; "
            "connect-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def load_record(key: str) -> Record:
        try:
            return catalog.load(key)
        except (OSError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc

    def load_document(key: str, doc: str = "") -> tuple[Record, Trajectory]:
        record = load_record(key)
        if not record.trajectory:
            raise HTTPException(422, record.error or "The file is not a supported ATIF trajectory.")
        try:
            return record, select_document(record.trajectory, doc)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    def selected_field(document: Trajectory, step: int, field: str) -> str | list[ContentPart] | None:
        if step < 1 or step > len(document.steps):
            raise HTTPException(404, "The step does not exist.")
        try:
            return field_value(document.steps[step - 1], field)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/download")
    def download(run: str):
        record = load_record(run)
        return FileResponse(record.path, media_type="application/json", filename=record.path.name)

    @app.get("/jump")
    def jump(request: Request, focus: int = 1):
        values = {key: value for key, value in request.query_params.items() if key not in TRACE_FILTERS}
        focus = max(1, focus)
        return RedirectResponse(query_url(values, focus=focus) + f"#step-{focus}", status_code=303)

    @app.get("/text")
    def full_text(request: Request, run: str, doc: str = "", step: int = 1, field: str = "message"):
        _, document = load_document(run, doc)
        value = selected_field(document, step, field)
        back_query = {key: value for key, value in request.query_params.items() if key not in ("step", "field")}
        return templates.TemplateResponse(
            request=request,
            name="text.html",
            context={
                "text": content_text(value),
                "step": step,
                "field": field,
                "back": query_url(back_query, run=run, doc=doc, focus=step) + f"#step-{step}",
            },
        )

    @app.get("/image")
    def image_file(run: str, doc: str, step: int, field: str, part: int):
        record, document = load_document(run, doc)
        value = selected_field(document, step, field)
        if not isinstance(value, list) or part < 0 or part >= len(value) or value[part].type != "image":
            raise HTTPException(404, "The image reference does not exist.")
        image = value[part].source
        path = (record.path.parent / image.path).resolve()
        if not path.is_relative_to(catalog.root) or not path.is_file():
            raise HTTPException(404, "The image is outside the selected directory or no longer exists.")
        if path.stat().st_size > 16 * 1024 * 1024:
            raise HTTPException(413, "The image exceeds the 16 MiB viewer limit.")
        with path.open("rb") as stream:
            header = stream.read(12)
        signatures = {
            "image/png": header.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": header.startswith(b"\xff\xd8\xff"),
            "image/gif": header.startswith((b"GIF87a", b"GIF89a")),
            "image/webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
        }
        if not signatures.get(image.media_type):
            raise HTTPException(415, "The file does not match the recorded image type.")
        return FileResponse(path, media_type=image.media_type)

    @app.get("/")
    def index(request: Request):
        query = dict(request.query_params)
        runs = catalog.runs()
        visible = filter_runs(runs, query)
        key = query.get("run") or (runs[0].key if catalog.single_file and runs else "")
        if key:
            query["run"] = key
        selected = next((run for run in runs if run.key == key), None)
        context = {
            "request": request,
            "root": catalog.root,
            "query": query,
            "url": partial(query_url, query),
            "browse_url": partial(query_url, query, **dict.fromkeys(READER_FIELDS)),
            "total": len(runs),
            "matched": len(visible),
            "selected": selected,
            "error": None,
            "document": None,
            "doc_key": query.get("doc", ""),
            "single_file": bool(catalog.single_file),
        }
        if not key:
            result_scope = filter_runs(runs, {**query, "mitigation": "", "status": ""})
            groups = (
                fault_groups(visible, query.get("sort", "name"))
                if query.get("campaign")
                else campaign_groups(visible, query.get("sort", "recent"))
            )
            pages = max(1, (len(groups) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(positive_integer(query.get("run_page")), pages)
            context.update(
                agents=sorted({run.agent for run in runs}),
                models=sorted({run.model for run in runs}),
                statuses=sorted({run.status for run in runs}),
                groups=groups[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
                group_count=len(groups),
                run_page=page,
                run_pages=pages,
                campaign_count=len({run.campaign for run in runs}),
                mitigation_counts=grade_counts(result_scope, "mitigation"),
                incomplete_count=sum(run.status == "incomplete" for run in result_scope),
            )
            return templates.TemplateResponse(request=request, name="browse.html", context=context)

        if selected:
            query["campaign"] = selected.campaign
            visible = [run for run in visible if run.campaign == selected.campaign]
            context["siblings"] = sorted(
                [
                    run
                    for run in runs
                    if (run.campaign, run.name, run.agent, run.model)
                    == (selected.campaign, selected.name, selected.agent, selected.model)
                ],
                key=attempt_order,
            )
            position = next((i for i, run in enumerate(visible) if run.key == key), None)
            context["previous_run"] = visible[position - 1] if position is not None and position > 0 else None
            context["next_run"] = (
                visible[position + 1] if position is not None and position + 1 < len(visible) else None
            )
        try:
            record = catalog.load(key)
            if selected is None and record.trajectory:
                # A referenced file has no right to borrow its parent's evaluation.
                context["selected"] = Run(
                    key,
                    record.path.name,
                    record.trajectory.agent.name,
                    record.trajectory.agent.model_name or "Unknown",
                    campaign=query.get("campaign", "."),
                )
            context["error"] = record.error
            context["raw"] = pretty(record.data)[:PREVIEW_SIZE] if record.data else None
            if record.trajectory:
                context.update(TraceReader(record.trajectory, record.path, catalog, query).context())
        except (OSError, ValueError) as exc:
            context["error"] = str(exc)
        return templates.TemplateResponse(request=request, name="viewer.html", context=context)

    return app
