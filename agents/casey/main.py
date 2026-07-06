import os
import sys
from datetime import datetime, date, timezone, timedelta
from dotenv import load_dotenv
from integrations.sheets import get_active_jobs, parse_latest_date
from integrations.hubspot import (
    hs_available,
    update_deal_properties,
    create_note_on_deal,
)
from integrations.gmail import send_email
from integrations.slack import send_message, send_dm
from integrations.quickbooks import get_invoice_status_for_customer
from agents.casey.email_composer import compose_customer_update_email
from db.state_store import (
    upsert_active_job,
    get_jobs_due_for_update,
    get_all_active_jobs,
    set_update_sent,
    set_next_scheduled_update,
    set_escalation,
    get_summary,
    was_alert_sent_today,
    get_alert_sent_at_today,
    was_escalation_sent_recently,
    record_alert_sent,
    get_config,
    get_email_history,
    create_agent_run,
    append_agent_run_log,
    finish_agent_run,
    set_agent_run_summary,
    increment_send_count,
    save_email_thread_ids,
)
from utils.logger import get_logger
from config.loader import cfg

load_dotenv()
logger = get_logger("casey")

MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"

MOCK_JOB = {
    "client_name": "Mark Parkinson",
    "pm_name": "Laura Peña",
    "job_type": "Landscaping",
    "start_date": "2026-04-01",
    "estimated_start_window": "2026-04-15",
    "deposit_date": "2026-03-20",
    "sheet_last_contact": "2026-03-01",
    "last_pm_contact": None,
    "assigned_crew_sub": "Zeidy",
    "customer_phone": "555-0199",
    "email": "mark.parkinson@example.com",
    "to_collect": "",
    "total_project": "15000",
    "job_description": "Full front yard landscaping and irrigation",
    "estimator_name": "Josh",
    "client_mood": "happy",
    "complaint_note": "",
}


def normalize_pm_name(pm_name: str) -> str:
    """Normalize PM name for consistent matching; handles casing variants like FERNANDA."""
    name = pm_name.strip().title()
    if name.upper() == "FERNANDA":
        return "Fernanda"
    return name


def _get_pm_email(pm_name: str) -> str:
    """Look up PM's email from pm_config table."""
    normalized = normalize_pm_name(pm_name)
    try:
        from db.state_store import get_pm_list
        for pm in get_pm_list():
            if normalize_pm_name(pm.get("full_name", "")) == normalized:
                return pm.get("email", "")
    except Exception:
        pass
    return ""


def _get_sales_rep_email(estimator_name: str, hubspot_owner_name: str = "") -> str:
    """Look up sales rep's email by estimator name or owner name."""
    try:
        from db.state_store import get_sales_rep_list
        reps = get_sales_rep_list()
        # Try estimator_name match first
        for target in (estimator_name, hubspot_owner_name):
            if not target:
                continue
            target_lower = target.strip().lower()
            for rep in reps:
                if rep.get("name", "").strip().lower() == target_lower:
                    return rep.get("email", "")
                # Partial first-name match
                first = rep.get("name", "").split()[0].lower()
                if first and first in target_lower:
                    return rep.get("email", "")
    except Exception:
        pass
    return ""


def _determine_scenario(job: dict) -> str:
    """Determine email scenario: invoice_reminder > in_progress > not_started."""
    # To Start tab always gets not_started regardless of other fields
    if job.get("sheet_tab") == "to_start":
        return "not_started"

    # In Process: check QB invoice status first
    qb_status = job.get("qb_invoice_status")
    if qb_status and qb_status.get("days_overdue", 0) >= 60:
        return "invoice_reminder"

    if job.get("assigned_crew_sub", "").strip():
        return "in_progress"

    start_str = job.get("start_date", "").strip()
    if start_str:
        try:
            start = datetime.strptime(start_str, "%Y-%m-%d").date()
            if start <= date.today():
                return "in_progress"
        except Exception:
            pass

    return "not_started"


def _is_stalled_job(job: dict) -> bool:
    """In Process job whose start date has passed with no crew/sub confirmed."""
    if job.get("sheet_tab") != "in_process":
        return False
    start_str = job.get("start_date", "").strip()
    if not start_str:
        return False
    try:
        start = datetime.strptime(start_str, "%Y-%m-%d").date()
    except Exception:
        return False
    if start >= date.today():
        return False
    return not job.get("assigned_crew_sub", "").strip()


def _idempotent_skip(job: dict) -> bool:
    """Return True if we already sent an email today for this job."""
    sent = job.get("last_customer_update_sent")
    if sent is None:
        return False
    if isinstance(sent, date):
        return sent >= date.today()
    if isinstance(sent, str):
        try:
            return datetime.strptime(sent, "%Y-%m-%d").date() >= date.today()
        except Exception:
            pass
    return False


def _get_contact_date(client_name: str, pm_name: str, job: dict) -> "date | None":
    """Last PM↔customer contact date: Gmail history (cached by OCA) first, sheet fallback otherwise.

    `job` here is a casey_active_jobs row dict (from get_jobs_due_for_update),
    where the sheet's "Most Recent communication" value is stored under
    most_recent_contact — fall back to sheet_last_contact too in case a raw
    sheet job dict is ever passed in.
    """
    history = get_email_history(client_name, pm_name)
    if history and history.last_sent_at:
        return history.last_sent_at
    sheet_value = job.get("most_recent_contact") or job.get("sheet_last_contact", "")
    return parse_latest_date(sheet_value)


def calculate_days_since(date_value) -> "int | None":
    if not date_value:
        return None
    if isinstance(date_value, str):
        try:
            date_value = datetime.strptime(date_value, "%Y-%m-%d").date()
        except Exception:
            return None
    try:
        return (date.today() - date_value).days
    except Exception:
        return None


def build_escalation_slack_msg(job: dict, reason: str, last_contact: "date | None" = None) -> str:
    deal_id = job.get("hubspot_deal_id")
    portal_id = cfg("hubspot_portal_id") or "51566851"
    deal_url = f"https://app.hubspot.com/contacts/{portal_id}/deal/{deal_id}" if deal_id else "—"
    return (
        f"⚠️ *Escalation Required*\n"
        f"Customer: {job['client_name']}\n"
        f"PM: {job.get('pm_name') or 'Unknown'}\n"
        f"Reason: {reason}\n"
        f"Last contact: {last_contact or 'Unknown'}\n"
        f"HubSpot: {deal_url}"
    )


def _hs_field(config_key: str, fallback: str) -> str:
    """Get configured HubSpot property name, or fall back to the default."""
    val = get_config(config_key)
    return val.strip() if val and val.strip() else fallback


def _write_email_to_hubspot(job: dict, scenario: str, subject: str, next_update_date: "date | None" = None) -> None:
    """Write email send event back to HubSpot deal as a note + update scheduling fields."""
    if not hs_available():
        return
    deal_id = job.get("hubspot_deal_id")
    if not deal_id:
        return
    note_body = (
        f"Casey Email Sent [{scenario.upper()}]\n"
        f"To: {job.get('customer_email', '')}\n"
        f"Subject: {subject}\n"
        f"PM: {job.get('pm_name', '')}\n"
        f"Date: {date.today().isoformat()}"
    )
    create_note_on_deal(deal_id, note_body)

    props: dict = {
        _hs_field("hubspot_field_last_update_sent", "last_customer_update_sent"): date.today().isoformat(),
    }
    if next_update_date:
        props[_hs_field("hubspot_field_next_update", "next_scheduled_update")] = next_update_date.isoformat()
    update_deal_properties(deal_id, props)


def _write_escalation_to_hubspot(job: dict, reason: str) -> None:
    """Write escalation event to HubSpot deal."""
    if not hs_available():
        return
    deal_id = job.get("hubspot_deal_id")
    if not deal_id:
        return
    note_body = (
        f"Casey Escalation Triggered\n"
        f"Reason: {reason}\n"
        f"PM: {job.get('pm_name', '')}\n"
        f"Date: {date.today().isoformat()}"
    )
    create_note_on_deal(deal_id, note_body)
    update_deal_properties(deal_id, {
        "hs_priority": "high",
        _hs_field("hubspot_field_escalation_flag",   "escalation_flag"):   "true",
        _hs_field("hubspot_field_escalation_reason", "escalation_reason"): reason,
    })


def _checkpoint(run_id: "int | None", line: str) -> None:
    """Append a key log line to this run's agent_runs record, if one exists.

    `run_id` is None when nothing is tracking this run via the run-log table
    (shouldn't normally happen — run() always creates or reuses one).
    """
    if run_id is not None:
        append_agent_run_log(run_id, line + "\n")


def run() -> None:
    # AGENT_RUN_ID is set by the dashboard when it spawns this as a subprocess
    # (it already created the row and owns the live SSE log stream + final
    # status/finished_at) — reuse that row instead of creating a second one.
    # Cron/CLI invocations have no such env var, so they create their own.
    external_run_id = os.getenv("AGENT_RUN_ID")
    own_run = external_run_id is None
    run_id = int(external_run_id) if external_run_id else create_agent_run("casey")

    try:
        summary = _run(run_id)
        set_agent_run_summary(run_id, summary)
        if own_run:
            finish_agent_run(run_id, "success", summary)
    except Exception as e:
        logger.exception("Casey crashed")
        set_agent_run_summary(run_id, f"Error: {e}")
        if own_run:
            finish_agent_run(run_id, "error", f"Error: {e}")
        try:
            josh_id = cfg("slack_josh_user_id")
            if josh_id:
                timestamp = datetime.now(timezone.utc).isoformat()
                msg = (
                    f"🔴 Agent crash — Casey failed at {timestamp}\n"
                    f"Error: {e}\n"
                    f"The next scheduled run will retry automatically."
                )
                send_dm(josh_id, msg)
        except Exception as notify_err:
            print(f"Casey: failed to send crash notification: {notify_err}", file=sys.stderr)
        raise


def _run(run_id: "int | None" = None) -> str:
    if not cfg("slack_bot_token"):
        logger.error("Casey: slack_bot_token not configured in DB — aborting")
        return "Aborted: slack_bot_token not configured"
    if not cfg("google_sheets_id"):
        logger.error("Casey: google_sheets_id not configured in DB — aborting")
        return "Aborted: google_sheets_id not configured"

    JOSH_SLACK_USER_ID  = cfg("slack_josh_user_id")
    SLACK_DAILY_CHANNEL = cfg("slack_casey_channel") or "casey-daily"

    # Fix 2: global email pause switch — default to paused if not yet configured
    emails_paused = (get_config("casey_emails_paused") or "true").strip().lower() == "true"
    if emails_paused:
        logger.info("Casey: casey_emails_paused=true — emails will be logged but NOT sent this run")

    # Fix 1: weekends always skipped — BCS operates Mon-Fri Eastern Time
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() in (5, 6):
        logger.info("Casey weekend skip — no customer emails sent on weekends")
        send_message(SLACK_DAILY_CHANNEL, "ℹ️ Casey run skipped — no customer emails are sent on weekends.")
        return "Weekend skip — no emails sent"

    logger.info("Casey starting run")

    # ── Step 1: Sync sheet jobs to DB ────────────────────────────────────────
    if MOCK_MODE:
        logger.info("MOCK_MODE=true — using mock job")
        sheet_jobs = [MOCK_JOB]
    else:
        sheet_jobs = get_active_jobs()
        if not sheet_jobs:
            logger.error("No active jobs loaded from Google Sheet — aborting")
            return "Aborted: no active jobs loaded from sheet"

    logger.info(f"Sheet: {len(sheet_jobs)} active jobs loaded")
    _checkpoint(run_id, f"Sheet: {len(sheet_jobs)} active jobs loaded")

    try:
        from integrations.hubspot import get_all_owners, search_deals_by_client_name, get_contact_email_for_deal
        owners = get_all_owners() if not MOCK_MODE else {}
    except Exception:
        owners = {}

    synced = 0
    for job in sheet_jobs:
        client_name = job["client_name"]
        if not client_name.strip():
            continue

        hubspot_deal_id = None
        hubspot_owner_name = None
        customer_email = job.get("email", "").strip()

        if not MOCK_MODE and hs_available():
            try:
                hs_deals = search_deals_by_client_name(client_name)
                if hs_deals:
                    hs_deal = hs_deals[0]
                    hubspot_deal_id = hs_deal["id"]
                    owner_id = hs_deal["properties"].get("hubspot_owner_id")
                    hubspot_owner_name = owners.get(str(owner_id), owner_id) if owner_id else None
                    if not customer_email:
                        customer_email = get_contact_email_for_deal(hubspot_deal_id) or ""
            except Exception as e:
                logger.warning(f"Casey HubSpot lookup failed for {client_name}: {e}")

        upsert_active_job({
            "client_name":            client_name,
            "pm_name":                job.get("pm_name", ""),
            "job_type":               job.get("job_type", ""),
            "start_date":             job.get("start_date", ""),
            "deposit_date":           job.get("deposit_date", ""),
            "estimated_start_window": job.get("estimated_start_window", ""),
            "assigned_crew_sub":      job.get("assigned_crew_sub", ""),
            "last_pm_contact":        job.get("last_pm_contact"),
            "most_recent_contact":    job.get("sheet_last_contact", ""),
            "pm_communication_history": job.get("pm_communication_history", ""),
            "hubspot_deal_id":        hubspot_deal_id,
            "hubspot_owner_name":     hubspot_owner_name,
            "customer_email":         customer_email,
            "customer_phone":         job.get("customer_phone", ""),
            "client_mood":            job.get("client_mood", ""),
            "complaint_note":         job.get("complaint_note", ""),
            "job_description":        job.get("job_description", ""),
            "estimator_name":         job.get("estimator_name", ""),
            "to_collect":             job.get("to_collect", ""),
            "total_project":          job.get("total_project", ""),
            "sheet_tab":              job.get("sheet_tab", ""),
        })
        synced += 1

    logger.info(f"Synced {synced} jobs to database")
    _checkpoint(run_id, f"HubSpot sync: {synced} jobs synced to database")

    # ── Step 2: Process jobs due for email ───────────────────────────────────
    due_jobs = get_jobs_due_for_update()
    logger.info(f"Jobs due for update: {len(due_jobs)}")

    emails_sent = 0
    escalations = 0
    skipped = 0
    no_invoice_found: list[str] = []

    for job in due_jobs:
        client_name = job["client_name"]
        pm_name     = job.get("pm_name", "")
        job_id      = f"{client_name}|{pm_name}"

        # Fix 3: per-customer hold — skip ALL processing (no email, no escalation, no Slack)
        if job.get("comms_hold"):
            hold_reason = job.get("comms_hold_reason") or "no reason recorded"
            logger.info(f"HOLD {job_id} — reason: {hold_reason}")
            _checkpoint(run_id, f"HOLD {job_id} — reason: {hold_reason}")
            skipped += 1
            continue

        # Idempotency: skip if already emailed today
        if _idempotent_skip(job):
            logger.info(f"SKIP {job_id} — already emailed today")
            _checkpoint(run_id, f"SKIP {job_id} — already emailed today")
            skipped += 1
            continue

        customer_email = job.get("customer_email", "").strip()
        if not customer_email:
            logger.info(f"SKIP {job_id} — no customer email")
            _checkpoint(run_id, f"SKIP {job_id} — no customer email")
            skipped += 1
            continue

        contact_date = _get_contact_date(client_name, pm_name, job)
        days_since_contact = calculate_days_since(contact_date)

        # ── QB invoice status + scenario (computed before escalation checks) ──
        qb_status = get_invoice_status_for_customer(job["client_name"])
        job["qb_invoice_status"] = qb_status
        if not qb_status or not qb_status.get("found"):
            no_invoice_found.append(client_name)
        scenario = _determine_scenario(job)

        # ── Escalation checks (run first, even for never-processed jobs — a
        # newly onboarded job that's already stale must still escalate) ───────
        escalation_reason = None
        if days_since_contact is not None and days_since_contact >= 14:
            escalation_reason = f"No PM contact in {days_since_contact} days"
        elif _is_stalled_job(job):
            escalation_reason = "Stalled job — start date passed but no crew confirmed"

        if escalation_reason:
            if was_escalation_sent_recently(job_id, days=3):
                logger.info(f"SKIP escalation {job_id} — sent in last 3 days")
                _checkpoint(run_id, f"SKIP escalation {job_id} — sent in last 3 days")
                skipped += 1
            else:
                set_escalation(client_name, pm_name, escalation_reason)
                msg = build_escalation_slack_msg(job, escalation_reason, contact_date)
                if JOSH_SLACK_USER_ID:
                    send_dm(JOSH_SLACK_USER_ID, msg)
                record_alert_sent(job_id, "escalation", client_name)
                _write_escalation_to_hubspot(job, escalation_reason)
                logger.info(f"ESCALATION {job_id} reason={escalation_reason}")
                _checkpoint(run_id, f"ESCALATION {job_id} reason={escalation_reason}")
                escalations += 1
            continue

        # ── New-job intro: first time Casey has ever processed this job ────────
        if job.get("last_customer_update_sent") is None and job.get("next_scheduled_update") is None:
            deposit_date = parse_latest_date(job.get("deposit_date", ""))
            days_since_deposit = (date.today() - deposit_date).days if deposit_date else None
            if days_since_deposit is not None and 0 <= days_since_deposit <= 14:
                scenario = "new_job_intro"
            else:
                set_next_scheduled_update(client_name, pm_name, date.today() + timedelta(days=7))
                logger.info(f"SKIP {job_id} — deposit not within last 14 days, normal update scheduled in 7 days")
                _checkpoint(run_id, f"SKIP {job_id} — deposit not within last 14 days, normal update scheduled in 7 days")
                skipped += 1
                continue

        # ── Email send ────────────────────────────────────────────────────
        sent_at_today = get_alert_sent_at_today(job_id, "update_due")
        logger.info(
            f"Idempotency check for {client_name}: "
            f"casey_sent_alerts.update_due sent_at_today={sent_at_today}, "
            f"casey_active_jobs.last_customer_update_sent={job.get('last_customer_update_sent')}, "
            f"today={date.today()}"
        )
        if was_alert_sent_today(job_id, "update_due"):
            # Fix 4: increment duplicate count and log a visible WARNING
            count = increment_send_count(job_id, "update_due")
            logger.warning(f"DUPLICATE BLOCKED — {client_name} already received email today (attempt #{count})")
            _checkpoint(run_id, f"DUPLICATE BLOCKED — {client_name} already received email today (attempt #{count})")
            skipped += 1
            continue

        pm_email = _get_pm_email(pm_name) if pm_name else ""
        if not pm_email:
            logger.warning(f"SKIP {job_id} — PM {pm_name!r} has no email in pm_config")
            _checkpoint(run_id, f"SKIP {job_id} — PM {pm_name!r} has no email in pm_config")
            skipped += 1
            continue

        estimator = job.get("estimator_name", "")
        owner_name = job.get("hubspot_owner_name", "")
        cc_email = _get_sales_rep_email(estimator, owner_name)

        email_history = get_email_history(client_name, pm_name)
        email_snippets = email_history.email_snippets if email_history and email_history.email_snippets else ""

        composed = compose_customer_update_email(
            customer_name=client_name,
            pm_name=pm_name,
            job_type=job.get("job_type", ""),
            scenario=scenario,
            contractor=job.get("assigned_crew_sub", ""),
            notes=job.get("pm_communication_history", "") or job.get("pm_notes", ""),
            to_collect=job.get("to_collect", ""),
            job_description=job.get("job_description", ""),
            complaint_note=job.get("complaint_note", ""),
            client_mood=job.get("client_mood", ""),
            total_project=job.get("total_project", ""),
            estimator_name=estimator,
            sheet_tab=job.get("sheet_tab", ""),
            email_history=email_snippets,
        )

        subject   = composed.get("subject", "Project Update — Blue Collar Scholars")
        body_html = composed.get("body_html", "")

        # Fix 2: thread continuity — reuse the original subject so Gmail groups messages
        prior_thread_id  = job.get("last_email_thread_id") or ""
        prior_message_id = job.get("last_email_message_id") or ""
        prior_subject    = job.get("last_email_subject") or ""
        if prior_thread_id and prior_subject:
            subject = prior_subject

        # Fix 2: global email pause — log what would have been sent but do not call send_email
        if emails_paused:
            logger.info(f"PAUSED — would have sent [{scenario}] to {client_name} at {customer_email}")
            _checkpoint(run_id, f"PAUSED — would have sent [{scenario}] to {client_name} at {customer_email}")
            continue

        sent, returned_thread_id, returned_message_id = send_email(
            sender_email=pm_email,
            to_email=customer_email,
            subject=subject,
            body_html=body_html,
            cc_email=cc_email,
            thread_id=prior_thread_id,
            message_id=prior_message_id,
        )

        if sent:
            next_update = date.today() + timedelta(days=7)
            set_update_sent(client_name, pm_name)
            record_alert_sent(job_id, "update_due", client_name)
            save_email_thread_ids(client_name, pm_name, returned_thread_id, returned_message_id, subject)
            _write_email_to_hubspot(job, scenario, subject, next_update_date=next_update)
            logger.info(f"EMAIL SENT [{scenario}] {job_id} → {customer_email} (cc={cc_email})")
            _checkpoint(run_id, f"EMAIL SENT [{scenario}] {job_id} → {customer_email} (cc={cc_email})")
            emails_sent += 1
        else:
            logger.error(f"EMAIL FAILED {job_id} → {customer_email}")
            _checkpoint(run_id, f"ERROR: EMAIL FAILED {job_id} → {customer_email}")

    # ── Step 3: Daily summary ────────────────────────────────────────────────
    summary = get_summary()
    all_jobs = get_all_active_jobs()
    not_started_count = sum(1 for j in all_jobs if _determine_scenario(j) == "not_started")
    in_progress_count = sum(1 for j in all_jobs if _determine_scenario(j) == "in_progress")
    invoice_count     = sum(1 for j in all_jobs if _determine_scenario(j) == "invoice_reminder")

    summary_msg = (
        f"*Casey Daily Summary*\n"
        f"Active jobs tracked: {summary['total']}\n"
        f"  Not started: {not_started_count}  |  In progress: {in_progress_count}  |  Invoice pending: {invoice_count}\n"
        f"Emails sent today: {emails_sent}\n"
        f"Escalations triggered: {escalations}\n"
        f"Skipped: {skipped}\n"
        f"Up to date: {summary['up_to_date']}"
    )
    if no_invoice_found:
        summary_msg += (
            f"\n\n📋 No QB invoice found for: {', '.join(no_invoice_found)}\n"
            f"(These customers may not have an invoice in QuickBooks yet — no action needed)"
        )
    send_message(SLACK_DAILY_CHANNEL, summary_msg)
    logger.info("Casey run complete")

    run_summary = f"{emails_sent} emails sent, {escalations} escalations, {skipped} skipped"
    _checkpoint(run_id, f"Casey run complete — {run_summary}")
    return run_summary


if __name__ == "__main__":
    run()
