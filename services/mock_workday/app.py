"""Mock Workday — a tiny FastAPI + Jinja app that mimics the surface
shape of a real Workday tenant well enough for tasks #1 and #5.

It's intentionally trivial:
  - GET  /                      redirect to /directory
  - GET  /directory             search form
  - GET  /directory/search?q=   HTML list of matching employees
  - GET  /people/{employee_id}  HTML profile page (for task #1 extract)
  - GET  /forms/new             blank request form (for task #5)
  - POST /forms/submit          returns a confirmation page with a
                                 downloadable attachment URL

Runs standalone via: `uv run python services/mock_workday/app.py`
Port defaults to 8001 (so it doesn't clash with the main API on 8000).
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

_THIS = Path(__file__).resolve().parent
EMPLOYEES_PATH = _THIS / "employees.json"
TEMPLATES_DIR = _THIS / "templates"
ATTACHMENTS_DIR = _THIS / "attachments"
ATTACHMENTS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Mock Workday", version="0.1.0")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _load_employees() -> list[dict]:
    if EMPLOYEES_PATH.exists():
        return json.loads(EMPLOYEES_PATH.read_text())
    # Fall back to a tiny built-in list so tests pass without seeding.
    return [
        {"employee_id": "E-1001", "handle": "octocat",
         "name": "Octavius Cat", "title": "Staff Engineer",
         "department": "Platform", "email": "octocat@example.com"},
        {"employee_id": "E-1002", "handle": "defunkt",
         "name": "Chris Wanstrath", "title": "CEO",
         "department": "Executive", "email": "defunkt@example.com"},
        {"employee_id": "E-1003", "handle": "mojombo",
         "name": "Tom Preston-Werner", "title": "Founder",
         "department": "Engineering", "email": "mojombo@example.com"},
    ]


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/directory")


@app.get("/directory", response_class=HTMLResponse)
async def directory(request: Request):
    return templates.TemplateResponse(request, "directory.html", {"employees": _load_employees()})


@app.get("/directory/search", response_class=HTMLResponse)
async def directory_search(request: Request, q: str = ""):
    all_emp = _load_employees()
    qn = q.strip().lower()
    matches = [
        e for e in all_emp
        if qn in e["name"].lower()
        or qn in e.get("handle", "").lower()
        or qn in e.get("employee_id", "").lower()
    ] if qn else all_emp
    return templates.TemplateResponse(
        request, "search_results.html",
        {"query": q, "matches": matches},
    )


@app.get("/people/{employee_id}", response_class=HTMLResponse)
async def person(request: Request, employee_id: str):
    for e in _load_employees():
        if e["employee_id"] == employee_id:
            return templates.TemplateResponse(request, "person.html", {"employee": e})
    return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)


@app.get("/forms/new", response_class=HTMLResponse)
async def form_new(request: Request):
    return templates.TemplateResponse(request, "form_new.html", {})


@app.post("/forms/submit", response_class=HTMLResponse)
async def form_submit(
    request: Request,
    employee_id: str = Form(...),
    request_type: str = Form(...),
    start_date: str = Form(""),
    notes: str = Form(""),
):
    confirmation_id = "CONF-" + secrets.token_hex(6).upper()
    # Write a tiny attachment so the form flow has something to "download".
    attachment_name = f"{confirmation_id}.txt"
    (ATTACHMENTS_DIR / attachment_name).write_text(
        f"Confirmation {confirmation_id}\n"
        f"employee_id={employee_id}\n"
        f"request_type={request_type}\n"
        f"start_date={start_date}\n"
        f"notes={notes}\n"
    )
    return templates.TemplateResponse(
        request, "confirmation.html",
        {
            "confirmation_id": confirmation_id,
            "employee_id": employee_id,
            "request_type": request_type,
            "attachment_url": f"/attachments/{attachment_name}",
        },
    )


@app.get("/attachments/{name}")
async def attachment(name: str):
    # minimal path sanitization
    safe = name.replace("..", "").replace("/", "")
    p = ATTACHMENTS_DIR / safe
    if not p.exists():
        return HTMLResponse("<h1>404</h1>", status_code=404)
    return HTMLResponse(p.read_text(), media_type="text/plain")


@app.get("/health")
async def health():
    return {"ok": True, "employees": len(_load_employees())}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("MOCK_WORKDAY_HOST", "127.0.0.1"),
                port=int(os.environ.get("MOCK_WORKDAY_PORT", "8001")))
