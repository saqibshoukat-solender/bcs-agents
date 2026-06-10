from datetime import date
from typing import Any

from integrations.sheets import parse_latest_date
from integrations.quickbooks import get_invoice_status_for_customer
from utils.logger import get_logger

logger = get_logger("oca.checks")


def _get_pm_config() -> list:
    try:
        from db.state_store import get_pm_list
        return get_pm_list()
    except Exception as e:
        logger.warning(f"Could not load pm_config from DB: {e}")
        return []


def _pm_email(pm_name: str) -> str:
    for pm in _get_pm_config():
        if pm.get("full_name") == pm_name:
            return pm.get("email", "")
    return ""


def _known_pm_names() -> set:
    return {pm.get("full_name", "") for pm in _get_pm_config()}


def _flag(client_name: str, pm_name: str, flag_type: str, details: str, urgency: str) -> dict[str, Any]:
    return {
        "job_id": f"{client_name}|{pm_name}",
        "flag_type": flag_type,
        "details": details,
        "urgency": urgency,
        "client_name": client_name,
        "pm_name": pm_name,
        "pm_email": _pm_email(pm_name),
    }


def check_stale_records(sheet_jobs: list) -> list[dict[str, Any]]:
    """Flag jobs where last PM contact (most_recent_contact, falling back to PM history) is > 7 days ago."""
    today = date.today()
    flags = []
    for job in sheet_jobs:
        client_name = job["client_name"]
        pm_name = job.get("pm_name", "")

        last_pm_contact = job.get("last_pm_contact")  # already a date | None from sheets.py
        if last_pm_contact is None:
            continue

        days_since_pm = (today - last_pm_contact).days
        if days_since_pm <= 7:
            continue

        urgency = "urgent" if days_since_pm > 14 else "warning"
        flags.append(_flag(
            client_name, pm_name,
            "stale_record",
            f"No PM contact in {days_since_pm} days — last contact: {last_pm_contact}",
            urgency,
        ))
    logger.info(f"check_stale_records: {len(flags)} flags")
    return flags


def check_missing_pm(sheet_jobs: list) -> list[dict[str, Any]]:
    """Flag jobs with no recognised PM that start within 14 days."""
    today = date.today()
    flags = []
    for job in sheet_jobs:
        client_name = job["client_name"]
        pm_name = job.get("pm_name", "").strip()

        if pm_name and pm_name in _known_pm_names():
            continue

        # Find the earliest upcoming start date within 14 days
        use_date: date | None = None
        for raw_field in ("start_date", "estimated_start_window"):
            raw = job.get(raw_field, "")
            try:
                d = parse_latest_date(raw)
            except Exception:
                continue
            if d is None:
                continue
            days_away = (d - today).days
            if 0 <= days_away <= 14:
                if use_date is None or d < use_date:
                    use_date = d

        if use_date is None:
            continue

        flags.append(_flag(
            client_name, pm_name,
            "missing_pm",
            f"No PM assigned, job starts {use_date}",
            "urgent",
        ))
    logger.info(f"check_missing_pm: {len(flags)} flags")
    return flags


def check_unconfirmed_crew(sheet_jobs: list) -> list[dict[str, Any]]:
    """Flag jobs with no contractor assigned that start within 7 days."""
    today = date.today()
    flags = []
    for job in sheet_jobs:
        client_name = job["client_name"]
        pm_name = job.get("pm_name", "")

        if job.get("assigned_crew_sub", "").strip():
            continue

        use_date: date | None = None
        for raw_field in ("start_date", "estimated_start_window"):
            raw = job.get(raw_field, "")
            try:
                d = parse_latest_date(raw)
            except Exception:
                continue
            if d is None:
                continue
            days_away = (d - today).days
            if 0 <= days_away <= 7:
                if use_date is None or d < use_date:
                    use_date = d

        if use_date is None:
            continue

        flags.append(_flag(
            client_name, pm_name,
            "unconfirmed_crew",
            f"No contractor assigned, job starts {use_date}",
            "urgent",
        ))
    logger.info(f"check_unconfirmed_crew: {len(flags)} flags")
    return flags


def _is_nonzero_amount(value: str) -> bool:
    if not value or not value.strip():
        return False
    cleaned = value.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned) > 0
    except ValueError:
        return bool(cleaned)


def _check_sheet_balance(job: dict) -> "dict[str, Any] | None":
    """Flag a job with an outstanding sheet balance, deposit > 30 days ago, and no recent contact."""
    today = date.today()
    client_name = job["client_name"]
    pm_name = job.get("pm_name", "")

    to_collect = job.get("to_collect", "")
    if not _is_nonzero_amount(to_collect):
        return None

    try:
        deposit_date = parse_latest_date(job.get("deposit_date", ""))
    except Exception:
        return None
    if deposit_date is None:
        return None
    if (today - deposit_date).days <= 30:
        return None

    if job.get("most_recent_contact", "").strip():
        return None

    return _flag(
        client_name, pm_name,
        "dropped_invoice",
        f"Outstanding balance {to_collect}, deposit {deposit_date}, no recent contact",
        "warning",
    )


def check_dropped_invoices(sheet_jobs: list) -> list[dict[str, Any]]:
    """Flag jobs with an overdue QB invoice, falling back to the sheet balance check."""
    flags = []
    for job in sheet_jobs:
        sheet_flag = _check_sheet_balance(job)

        qb_status = get_invoice_status_for_customer(job["client_name"])
        qb_flag = None
        if qb_status and qb_status["status"] == "overdue" and qb_status["days_overdue"] > 30:
            qb_flag = _flag(
                job["client_name"], job.get("pm_name", ""),
                "dropped_invoice",
                f"QB invoice overdue {qb_status['days_overdue']} days — "
                f"${qb_status['amount_due']:,.2f} outstanding",
                "urgent" if qb_status["days_overdue"] > 60 else "warning",
            )

        if qb_flag:
            flags.append(qb_flag)
        elif sheet_flag:
            flags.append(sheet_flag)
    logger.info(f"check_dropped_invoices: {len(flags)} flags")
    return flags


def check_job_readiness(sheet_jobs: list, hs_deals: list) -> list[dict[str, Any]]:
    """Flag sheet jobs that have no matching HubSpot deal (data sync issue)."""
    hs_names = [
        d.get("properties", {}).get("dealname", "").lower()
        for d in hs_deals
    ]
    flags = []
    for job in sheet_jobs:
        client_name = job["client_name"]
        pm_name = job.get("pm_name", "")
        client_lower = client_name.lower()

        matched = any(
            client_lower in dn or dn in client_lower
            for dn in hs_names
            if dn
        )
        if matched:
            continue

        flags.append(_flag(
            client_name, pm_name,
            "readiness_sync",
            f"Job in sheet but no HubSpot deal found for '{client_name}'",
            "info",
        ))
    logger.info(f"check_job_readiness: {len(flags)} flags")
    return flags
