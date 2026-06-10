from datetime import date, datetime
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from db.state_store import (
    _Session, _db_available,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _status_for_job(job: dict) -> str:
    if job.get("escalation_flag"):
        return "Escalated"
    lpc = job.get("last_pm_contact")
    if lpc:
        if isinstance(lpc, str):
            try:
                lpc = datetime.strptime(lpc, "%Y-%m-%d").date()
            except Exception:
                lpc = None
        if lpc:
            days = (date.today() - lpc).days
            if days > 14:
                return "Stale"
            if days > 7:
                return "Due for Update"
    return "On Track"


def _get_jobs_from_db(to_start: bool) -> list[dict]:
    if not _db_available:
        return []
    try:
        from sqlalchemy import or_
        from db.state_store import CaseyActiveJob
        today_str = date.today().isoformat()
        target_tab = "to_start" if to_start else "in_process"
        with _Session() as s:
            rows = (
                s.query(CaseyActiveJob)
                .filter(or_(CaseyActiveJob.sheet_tab == target_tab, CaseyActiveJob.sheet_tab.is_(None)))
                .all()
            )
        result = []
        for r in rows:
            if r.sheet_tab is None:
                # Fallback for rows from before the sheet_tab migration
                sd = r.start_date or ""
                if to_start:
                    # jobs where start_date is null or in the future
                    if sd and sd <= today_str:
                        continue
                else:
                    # jobs that have started (start_date <= today)
                    if not sd or sd > today_str:
                        continue
            result.append({
                "id": r.id,
                "client_name": r.client_name,
                "pm_name": r.pm_name or "",
                "job_type": r.job_type or "",
                "start_date": r.start_date or "",
                "deposit_date": r.deposit_date or "",
                "estimated_start_window": r.estimated_start_window or "",
                "assigned_crew_sub": r.assigned_crew_sub or "",
                "last_pm_contact": str(r.last_pm_contact) if r.last_pm_contact else "—",
                "most_recent_contact": r.most_recent_contact or "",
                "hubspot_deal_id": r.hubspot_deal_id or "",
                "hubspot_owner_name": r.hubspot_owner_name or "",
                "customer_email": r.customer_email or "",
                "escalation_flag": r.escalation_flag,
                "escalation_reason": r.escalation_reason or "",
                "status": _status_for_job({
                    "escalation_flag": r.escalation_flag,
                    "last_pm_contact": r.last_pm_contact,
                }),
            })
        return result
    except Exception as e:
        return []


@router.get("/jobs/to-start", response_class=HTMLResponse)
async def jobs_to_start(request: Request):
    jobs = _get_jobs_from_db(to_start=True)
    return templates.TemplateResponse(request, "jobs.html", {
        "page": "to-start",
        "title": "Jobs — To Start",
        "view": "to_start",
        "jobs": jobs,
        "empty_msg": "No upcoming jobs found in the local database.",
    })


@router.get("/jobs/in-process", response_class=HTMLResponse)
async def jobs_in_process(request: Request):
    jobs = _get_jobs_from_db(to_start=False)
    return templates.TemplateResponse(request, "jobs.html", {
        "page": "in-process",
        "title": "Jobs — In Process",
        "view": "in_process",
        "jobs": jobs,
        "empty_msg": "No jobs in progress in the local database.",
    })


@router.post("/jobs/delete")
async def delete_jobs():
    """Delete all jobs, escalations, flags, and alert history. Config tables are preserved."""
    if not _db_available:
        return JSONResponse({"error": "Database not available", "deleted": 0})

    try:
        from db.state_store import (
            CaseyActiveJob, OcaFlag, CaseySentAlert, OcaRun,
        )
        with _Session() as s:
            jobs_deleted = s.query(CaseyActiveJob).delete()
            s.query(OcaFlag).delete()
            s.query(CaseySentAlert).delete()
            s.query(OcaRun).delete()
            s.commit()
        return JSONResponse({"deleted": jobs_deleted})
    except Exception as e:
        return JSONResponse({"error": str(e), "deleted": 0})
