from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from db.state_store import get_run_logs, get_agent_run

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _fmt_run_row(run: dict) -> dict:
    started = run.get("started_at")
    finished = run.get("finished_at")

    started_str = started.strftime("%b %d, %Y %I:%M %p") if started else "—"

    duration_str = "Running…"
    if started and finished:
        seconds = int((finished - started).total_seconds())
        minutes, secs = divmod(max(seconds, 0), 60)
        duration_str = f"{minutes}m {secs}s" if minutes else f"{secs}s"

    return {
        "id": run["id"],
        "agent": run["agent"],
        "started_str": started_str,
        "duration_str": duration_str,
        "status": run.get("status", "running"),
        "summary": run.get("summary") or "",
    }


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request, agent: str = ""):
    agent_filter = agent if agent in ("casey", "oca") else None
    runs = [_fmt_run_row(r) for r in get_run_logs(agent=agent_filter, limit=100)]
    return templates.TemplateResponse(request, "runs.html", {
        "page": "runs",
        "runs": runs,
        "agent_filter": agent_filter or "",
    })


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail_page(request: Request, run_id: int):
    run = get_agent_run(run_id)
    if not run:
        return HTMLResponse("<h1>Run not found</h1>", status_code=404)
    row = _fmt_run_row(run)
    row["log"] = run.get("log") or "(no log output captured)"
    return templates.TemplateResponse(request, "run_detail.html", {
        "page": "runs",
        "run": row,
    })


@router.get("/api/runs/latest")
async def api_runs_latest():
    """Last 5 runs per agent, for dashboard home page status cards."""
    result = {}
    for agent in ("casey", "oca"):
        runs = get_run_logs(agent=agent, limit=5)
        result[agent] = [
            {
                "id": r["id"],
                "status": r["status"],
                "summary": r.get("summary") or "",
                "started_at": r["started_at"].isoformat() if r.get("started_at") else None,
                "finished_at": r["finished_at"].isoformat() if r.get("finished_at") else None,
            }
            for r in runs
        ]
    return JSONResponse(result)
