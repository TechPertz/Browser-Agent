"""HTML routes rendering HTMX-driven Jinja templates.

All pages consume the same JSON endpoints as any other client — the
templates are thin view-models over /api/*. Adding a Next.js frontend
later would just duplicate the consumption layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import uuid

import yaml
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from andera.credentials import SealedStateStore

from ..registry import get_registry

router = APIRouter()

_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/ui/runs", status_code=302)


@router.get("/ui/runs", response_class=HTMLResponse)
async def runs_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "runs.html",
        {"runs": get_registry().list()},
    )


@router.get("/ui/runs/fragment", response_class=HTMLResponse)
async def runs_fragment(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "runs_fragment.html",
        {"runs": get_registry().list()},
    )


@router.get("/ui/runs/new", response_class=HTMLResponse)
async def new_run_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "new_run.html", {})


@router.post("/ui/runs/create")
async def create_run_from_form(
    request: Request,
    prompt: str = Form(...),
    repeat: str = Form(""),
    max_samples: str = Form(""),
    extract_fields: str = Form(""),
    multi_item: str = Form(""),
    input_file: UploadFile | None = File(None),
) -> Any:
    """Accept the NLP-first form: natural-language task + optional upload.

    When `repeat` is checked, the uploaded file is persisted under
    runs/<run_id>/input.<ext> and its path is fed to the JSON API. This
    keeps a single source of truth — the JSON API does the heavy work;
    this handler just adapts multipart input into that shape.
    """
    from .runs import CreateRunRequest, create_run

    repeat_flag = repeat.strip().lower() in ("true", "on", "1", "yes")
    multi_item_flag = multi_item.strip().lower() in ("true", "on", "1", "yes")
    max_n = int(max_samples) if max_samples.strip() else None
    fields_str = extract_fields.strip() or None

    # Pre-allocate a run_id so we can place the uploaded file under it.
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    input_path_str: str | None = None

    if repeat_flag:
        if input_file is None or not input_file.filename:
            return templates.TemplateResponse(
                request, "new_run.html",
                {"error": "repeat mode requires an input file"},
                status_code=400,
            )
        data = await input_file.read()
        if not data:
            return templates.TemplateResponse(
                request, "new_run.html",
                {"error": "uploaded input file is empty"},
                status_code=400,
            )
        ext = Path(input_file.filename).suffix or ".csv"
        run_dir = Path("runs") / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        saved = run_dir / f"input{ext}"
        saved.write_bytes(data)
        input_path_str = str(saved)

    try:
        result = await create_run(CreateRunRequest(
            prompt=prompt,
            input_path=input_path_str,
            repeat=repeat_flag,
            max_samples=max_n,
            run_id=run_id,
            extract_fields=fields_str,
            multi_item=multi_item_flag,
        ))
    except HTTPException as e:
        return templates.TemplateResponse(
            request, "new_run.html",
            {"error": e.detail},
            status_code=e.status_code,
        )
    return RedirectResponse(f"/ui/runs/{result['run_id']}", status_code=303)


@router.get("/ui/runs/{run_id}", response_class=HTMLResponse)
async def run_detail_page(request: Request, run_id: str) -> HTMLResponse:
    rec = get_registry().get(run_id)
    if rec is None:
        raise HTTPException(404, "run not found")
    return templates.TemplateResponse(
        request, "run_detail.html",
        {"run": rec.public_dict()},
    )


@router.get("/ui/runs/{run_id}/samples/fragment", response_class=HTMLResponse)
async def samples_fragment(request: Request, run_id: str) -> HTMLResponse:
    rec = get_registry().get(run_id)
    if rec is None:
        raise HTTPException(404, "run not found")
    return templates.TemplateResponse(
        request, "samples_fragment.html",
        {"run": rec.public_dict(), "samples": rec.samples},
    )


@router.get("/ui/runs/{run_id}/samples/{sample_id}", response_class=HTMLResponse)
async def sample_detail_page(
    request: Request, run_id: str, sample_id: str,
) -> HTMLResponse:
    rec = get_registry().get(run_id)
    if rec is None:
        raise HTTPException(404, "run not found")
    sample = next((s for s in rec.samples if s.get("sample_id") == sample_id), None)
    if sample is None:
        raise HTTPException(404, "sample not found")

    # Enrich from on-disk manifest if present (extracted + artifacts are there).
    extracted: dict = {}
    artifacts: list[dict] = []
    if rec.run_root:
        mpath = Path(rec.run_root) / "RUN_MANIFEST.json"
        if mpath.exists():
            try:
                m = json.loads(mpath.read_text())
                for s in m.get("samples", []):
                    if s.get("sample_id") == sample_id:
                        extracted = s.get("extracted") or {}
                        break
                # Artifacts not tied to sample in manifest yet; surface all.
                # Manifest stores {sha256, path, size}; template expects
                # `.name` so derive it from the path basename.
                raw_artifacts = m.get("artifacts") or []
                for a in raw_artifacts:
                    a = dict(a)
                    if "name" not in a and a.get("path"):
                        a["name"] = a["path"].rsplit("/", 1)[-1]
                    artifacts.append(a)
            except Exception:
                pass

    return templates.TemplateResponse(
        request, "sample_detail.html",
        {
            "run_id": run_id,
            "sample": sample,
            "extracted_json": json.dumps(extracted, indent=2),
            "artifacts": artifacts,
        },
    )


@router.get("/ui/connections", response_class=HTMLResponse)
async def connections_page(request: Request) -> HTMLResponse:
    hosts = SealedStateStore().list_hosts()
    return templates.TemplateResponse(request, "connections.html", {"hosts": hosts})
