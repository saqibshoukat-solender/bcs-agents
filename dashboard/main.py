from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, Response

BASE_DIR = Path(__file__).parent

app = FastAPI(title="BCS Agents Dashboard")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

from dashboard.routes.jobs import router as jobs_router
from dashboard.routes.config import router as config_router
from dashboard.routes.agents import router as agents_router
from dashboard.routes.runs import router as runs_router
from dashboard.routes.auth import router as auth_router

app.include_router(jobs_router)
app.include_router(config_router)
app.include_router(agents_router)
app.include_router(runs_router)
app.include_router(auth_router)

_PUBLIC_PATHS = {"/login"}
_PUBLIC_PREFIXES = ("/static/",)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)

    from dashboard.auth import get_current_user
    user = get_current_user(request)
    if not user:
        if request.headers.get("hx-request") == "true":
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=302)

    request.state.user = user
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    from db.state_store import get_summary, get_last_agent_run, _db_available
    from db.state_store import _Session, AgentRun, OcaFlag

    # Real stats from DB
    summary = get_summary()

    oca_active = 0
    if _db_available:
        try:
            with _Session() as s:
                oca_active = s.query(OcaFlag).filter(OcaFlag.resolved_at == None).count()
        except Exception:
            pass

    escalated = summary.get("escalated", 0)
    due = summary.get("due_for_update", 0)
    total = summary.get("total", 0)

    stats = {
        "total_active_jobs":   total,
        "jobs_due_for_update": due,
        "escalations_this_week": escalated,
        "oca_flags_active":    oca_active,
    }

    def _fmt_run(run):
        if not run:
            return None
        ts = run.get("started_at")
        if ts:
            ts = ts.strftime("%b %d, %Y at %I:%M %p") if hasattr(ts, "strftime") else str(ts)
        return {
            "date": ts or "Never",
            "status": run.get("status", ""),
            "id": run.get("id"),
            "summary": run.get("summary") or "",
        }

    casey_run = _fmt_run(get_last_agent_run("casey"))
    oca_run   = _fmt_run(get_last_agent_run("oca"))

    return templates.TemplateResponse(request, "dashboard.html", {
        "page":       "dashboard",
        "stats":      stats,
        "casey_run":  casey_run,
        "oca_run":    oca_run,
    })
