import sys
from collections import defaultdict
from datetime import datetime, timezone

from slack_sdk import WebClient

from agents.oca.checks import (
    check_stale_records,
    check_missing_pm,
    check_unconfirmed_crew,
    check_dropped_invoices,
    check_job_readiness,
)
from agents.oca.alerts import (
    route_alert,
    route_combined_alert,
    send_daily_digest,
    send_weekly_summary,
    escalate_unresolved_warning,
)
from db.state_store import (
    init_db,
    flag_exists,
    create_flag,
    should_alert_again,
    update_flag_alerted,
    get_flag_alert_age_hours,
    resolve_flags_not_in,
    get_active_flags_summary,
    get_weekly_summary,
    is_first_run_today,
    upsert_active_job,
    set_config,
    get_config,
    set_hubspot_deal_id,
    set_hubspot_contact_id,
)
from integrations.slack import send_dm
from integrations.sheets import get_active_jobs
from integrations.hubspot import (
    DEPOSIT_INVOICE_STAGE_ID,
    get_open_deals,
    create_deal,
    get_all_owners,
    hs_available,
    search_deals_by_client_name,
    search_contact_by_email,
    search_contact_by_name,
    create_contact,
    associate_contact_to_deal,
    update_deal_properties,
    create_note_on_deal,
)
from utils.logger import get_logger
from config.loader import cfg

logger = get_logger("oca")

_ALL_FLAG_TYPES = [
    "stale_record",
    "missing_pm",
    "unconfirmed_crew",
    "dropped_invoice",
    "readiness_sync",
]


def _get_hs_custom_field_map() -> dict:
    """Returns {job_dict_key: hs_property_internal_name} from DB config.
    These are the 6 fields that OCA writes back to HubSpot deals on every sync.
    """
    keys = [
        ("pm_name",                   "hubspot_field_pm_name"),
        ("assigned_crew_sub",         "hubspot_field_crew_confirmed"),
        ("last_customer_update_sent", "hubspot_field_last_update_sent"),
        ("next_scheduled_update",     "hubspot_field_next_update"),
        ("escalation_flag",           "hubspot_field_escalation_flag"),
        ("escalation_reason",         "hubspot_field_escalation_reason"),
    ]
    result = {}
    for field_key, config_key in keys:
        hs_name = get_config(config_key)
        if hs_name and hs_name.strip():
            result[field_key] = hs_name.strip()
    return result


def _sync_all(sheet_jobs: list, hs_deals: list) -> None:
    """
    OCA sync: keep Google Sheet, HubSpot, and local DB aligned.

    Deal/contact resolution order per job (stops at first hit):
      Layer 1 — DB: if casey_active_jobs already has hubspot_deal_id / hubspot_contact_id, use it.
      Layer 2 — HubSpot search: search_deals_by_client_name / search_contact_by_email
                / search_contact_by_name. If found, persist the ID to the DB immediately.
      Layer 3 — create: if nothing found, create a new deal/contact and persist its ID
                immediately so the next job (or the next run) sees it via Layer 1.
    """
    pipeline_id = cfg("hubspot_pipeline_id") or "default"

    # Pre-load all DB jobs for Layer 1 lookup (one query, keyed by client_name)
    from db.state_store import get_all_active_jobs
    db_map: dict[str, dict] = {j["client_name"]: j for j in get_all_active_jobs()}

    try:
        owners = get_all_owners()
    except Exception:
        owners = {}

    try:
        from db.state_store import get_sales_rep_list
        sales_reps = get_sales_rep_list()
    except Exception:
        sales_reps = []

    custom_field_map = _get_hs_custom_field_map()

    synced = 0
    created_deals = 0
    created_contacts = 0
    errors = 0

    for job in sheet_jobs:
        client_name = job["client_name"]
        if not client_name.strip():
            continue

        pm_name = job.get("pm_name", "")
        job_type = job.get("job_type", "").strip()
        deal_name = f"{client_name} — {job_type}" if job_type else client_name

        hubspot_deal_id: "str | None" = None
        hubspot_owner_name: str = ""
        hubspot_contact_id: "str | None" = None
        contact_needs_association = False

        # ── Deal Layer 1: DB check ───────────────────────────────────────────
        existing_db = db_map.get(client_name)
        if existing_db:
            if existing_db.get("hubspot_deal_id"):
                hubspot_deal_id = existing_db["hubspot_deal_id"]
                hubspot_owner_name = existing_db.get("hubspot_owner_name") or ""
                logger.info(f"Using existing deal from DB for {client_name}: {hubspot_deal_id}")
            if existing_db.get("hubspot_contact_id"):
                hubspot_contact_id = existing_db["hubspot_contact_id"]

        # ── Deal Layer 2: HubSpot search ─────────────────────────────────────
        if not hubspot_deal_id and hs_available():
            found = search_deals_by_client_name(client_name)
            if found:
                hubspot_deal_id = found[0]["id"]
                owner_id = found[0].get("properties", {}).get("hubspot_owner_id", "")
                hubspot_owner_name = owners.get(str(owner_id), "") if owner_id else ""
                logger.info(f"Found existing HubSpot deal for {client_name}: {hubspot_deal_id}")
                set_hubspot_deal_id(client_name, pm_name, hubspot_deal_id)

        # ── Deal Layer 3: create ──────────────────────────────────────────────
        if not hubspot_deal_id and hs_available():
            owner_id = _find_owner_for_job(job, sales_reps)
            amount_raw = job.get("total_project", "").replace("$", "").replace(",", "").strip()
            try:
                amount = str(float(amount_raw)) if amount_raw else ""
            except ValueError:
                amount = ""
            new_id = create_deal(
                dealname=deal_name,
                pipeline_id=pipeline_id,
                stage_id=DEPOSIT_INVOICE_STAGE_ID,
                amount=amount,
                owner_id=owner_id,
            )
            if new_id:
                hubspot_deal_id = new_id
                created_deals += 1
                set_hubspot_deal_id(client_name, pm_name, new_id)
                contact_needs_association = True

        # ── Contact Layer 2/3: search then create ────────────────────────────
        if not hubspot_contact_id and hs_available():
            email = job.get("email", "").strip()
            phone = job.get("customer_phone", "").strip()
            parts = client_name.strip().split(None, 1)
            first = parts[0] if parts else client_name
            last = parts[1] if len(parts) > 1 else ""

            found_cid = None
            if email:
                found_cid = search_contact_by_email(email)
            if not found_cid:
                found_cid = search_contact_by_name(first, last)

            if found_cid:
                hubspot_contact_id = found_cid
                logger.info(f"Found existing HubSpot contact for {client_name}: {hubspot_contact_id}")
                set_hubspot_contact_id(client_name, pm_name, hubspot_contact_id)
                contact_needs_association = True
            else:
                new_cid = create_contact(first, last, email, phone)
                if new_cid:
                    hubspot_contact_id = new_cid
                    created_contacts += 1
                    logger.info(f"Created HubSpot contact for {client_name}: {hubspot_contact_id}")
                    set_hubspot_contact_id(client_name, pm_name, hubspot_contact_id)
                    contact_needs_association = True

        # ── Associate contact ↔ deal whenever either side was newly resolved ─
        if hubspot_contact_id and hubspot_deal_id and contact_needs_association:
            associate_contact_to_deal(hubspot_contact_id, hubspot_deal_id)

        # ── Write pm_name (text) + crew_confirmed (boolean) to deal ─────────
        if hubspot_deal_id and hs_available() and custom_field_map:
            props = {}
            pm_prop = custom_field_map.get("pm_name")
            if pm_prop:
                pm_val = (job.get("pm_name") or "").strip()
                if pm_val:
                    props[pm_prop] = pm_val
            crew_prop = custom_field_map.get("assigned_crew_sub")
            if crew_prop:
                props[crew_prop] = "true" if (job.get("assigned_crew_sub") or "").strip() else "false"
            if props:
                update_deal_properties(hubspot_deal_id, props)

        # ── Upsert to DB ─────────────────────────────────────────────────────
        try:
            upsert_active_job({
                "client_name":            client_name,
                "pm_name":                pm_name,
                "job_type":               job_type,
                "start_date":             job.get("start_date", ""),
                "deposit_date":           job.get("deposit_date", ""),
                "estimated_start_window": job.get("estimated_start_window", ""),
                "assigned_crew_sub":      job.get("assigned_crew_sub", ""),
                "last_pm_contact":        job.get("last_pm_contact"),
                "most_recent_contact":    job.get("most_recent_contact", ""),
                "pm_communication_history": job.get("pm_communication_history", ""),
                "hubspot_deal_id":        hubspot_deal_id,
                "hubspot_contact_id":     hubspot_contact_id,
                "hubspot_owner_name":     hubspot_owner_name,
                "customer_email":         job.get("email", ""),
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
        except Exception as e:
            logger.error(f"OCA sync DB error for {client_name}: {e}")
            errors += 1

    set_config("last_sync_at", datetime.utcnow().isoformat())
    logger.info(
        f"OCA sync complete — sheet jobs: {len(sheet_jobs)}, "
        f"DB upserted: {synced}, HubSpot deals created: {created_deals}, "
        f"HubSpot contacts created: {created_contacts}, errors: {errors}"
    )


def _find_owner_for_job(job: dict, sales_reps: list) -> str:
    # Match by estimator name first, fall back to pm_name
    for name_field in ("estimator_name", "pm_name"):
        target = (job.get(name_field) or "").lower()
        if not target:
            continue
        for rep in sales_reps:
            first = rep.get("name", "").split()[0].lower()
            if first and first in target:
                return rep.get("hubspot_owner_id", "")
    return ""


def run() -> None:
    try:
        _run()
    except Exception as e:
        logger.exception("OCA crashed")
        try:
            josh_id = cfg("slack_josh_user_id")
            if josh_id:
                timestamp = datetime.now(timezone.utc).isoformat()
                msg = (
                    f"🔴 Agent crash — OCA failed at {timestamp}\n"
                    f"Error: {e}\n"
                    f"The next scheduled run will retry automatically."
                )
                send_dm(josh_id, msg)
        except Exception as notify_err:
            print(f"OCA: failed to send crash notification: {notify_err}", file=sys.stderr)
        raise


def _run() -> None:
    if not cfg("slack_bot_token"):
        logger.error("OCA: slack_bot_token not configured in DB — aborting")
        return
    if not cfg("google_sheets_id"):
        logger.error("OCA: google_sheets_id not configured in DB — aborting")
        return
    if not cfg("hubspot_access_token"):
        logger.error("OCA: hubspot_access_token not configured in DB — aborting")
        return

    logger.info("OCA starting run")
    init_db()

    from db.state_store import get_pm_list
    pm_config = get_pm_list()

    josh_slack_id = cfg("slack_josh_user_id")
    sam_slack_id  = cfg("slack_sam_user_id")
    slack_client  = WebClient(token=cfg("slack_bot_token"))

    # ── Step 1: Fetch latest data ────────────────────────────────────────────
    sheet_jobs = get_active_jobs()
    if not sheet_jobs:
        logger.error("OCA: No sheet jobs loaded — aborting")
        return

    hs_deals = get_open_deals()
    logger.info(f"OCA loaded {len(sheet_jobs)} sheet jobs, {len(hs_deals)} HubSpot deals")

    # ── Step 2: Sync — keep Sheet, HubSpot, and local DB aligned ────────────
    _sync_all(sheet_jobs, hs_deals)

    # Re-fetch open deals — _sync_all() may have just created new deals, and
    # check_job_readiness() needs the up-to-date list to avoid false positives
    # on the very run that created them.
    hs_deals = get_open_deals()

    # ── Step 3: Run checks ───────────────────────────────────────────────────
    all_flags: list[dict] = []
    all_flags.extend(check_stale_records(sheet_jobs))
    all_flags.extend(check_missing_pm(sheet_jobs))
    all_flags.extend(check_unconfirmed_crew(sheet_jobs))
    all_flags.extend(check_dropped_invoices(sheet_jobs))
    all_flags.extend(check_job_readiness(sheet_jobs, hs_deals))

    logger.info(f"OCA detected {len(all_flags)} flags total")

    # ── Step 4: Process flags with deduplication + cooldown ─────────────────
    alerted = 0
    suppressed = 0

    # Build a lookup of deal_id for each client_name for HS note writing
    from db.state_store import get_all_active_jobs
    db_jobs = {j["client_name"]: j for j in get_all_active_jobs()}

    # Group flags by job_id so multi-issue jobs get one combined Slack message
    flags_by_job: dict[str, list[dict]] = defaultdict(list)
    for flag in all_flags:
        flags_by_job[flag["job_id"]].append(flag)

    for job_id, job_flags in flags_by_job.items():
        active_flags: list[dict] = []
        for flag in job_flags:
            flag_type = flag["flag_type"]
            client_name = flag.get("client_name", job_id.split("|")[0])
            urgency = flag.get("urgency", "")
            flag["hubspot_deal_id"] = db_jobs.get(client_name, {}).get("hubspot_deal_id")

            if flag_exists(job_id, flag_type):
                if should_alert_again(job_id, flag_type, cooldown_hours=24):
                    # Gap 2: warning flags still unresolved >48h after the last
                    # alert get an extra escalation straight to Josh.
                    if urgency == "warning":
                        age_hours = get_flag_alert_age_hours(job_id, flag_type)
                        if age_hours is not None and age_hours > 48:
                            escalate_unresolved_warning(flag, int(age_hours), slack_client, josh_slack_id)
                    update_flag_alerted(job_id, flag_type)
                    _write_flag_to_hubspot(flag, db_jobs.get(client_name, {}))
                    active_flags.append(flag)
                    alerted += 1
                else:
                    logger.info(f"OCA suppressed (cooldown) {flag_type} for {job_id}")
                    suppressed += 1
            else:
                create_flag(job_id, flag_type, flag.get("details", ""), flag.get("urgency", ""))
                _write_flag_to_hubspot(flag, db_jobs.get(client_name, {}))
                active_flags.append(flag)
                alerted += 1

        if not active_flags:
            continue

        if len(active_flags) == 1:
            route_alert(active_flags[0], slack_client, pm_config, josh_slack_id, sam_slack_id)
        else:
            route_combined_alert(active_flags, slack_client, pm_config, josh_slack_id, sam_slack_id)

    # ── Step 5: Auto-resolve flags ───────────────────────────────────────────
    for flag_type in _ALL_FLAG_TYPES:
        flagged_ids = [f["job_id"] for f in all_flags if f["flag_type"] == flag_type]
        resolve_flags_not_in(flagged_ids, flag_type)

    # ── Step 6: Daily digest + weekly summary ────────────────────────────────
    summary   = get_active_flags_summary()
    first_run = is_first_run_today()
    if first_run:
        send_daily_digest(summary, slack_client, sam_slack_id)

    if datetime.now().weekday() == 0 and first_run:
        weekly = get_weekly_summary()
        send_weekly_summary(weekly, slack_client, josh_slack_id)

    logger.info(f"OCA run complete — alerted: {alerted}, suppressed: {suppressed}")


def _write_flag_to_hubspot(flag: dict, db_job: dict) -> None:
    """Write a HubSpot note for this flag on the associated deal."""
    if not hs_available():
        return
    deal_id = db_job.get("hubspot_deal_id")
    if not deal_id:
        return
    flag_type = flag.get("flag_type", "flag")
    details   = flag.get("details", "")
    urgency   = flag.get("urgency", "")
    client_name = flag.get("client_name", "")
    pm_name   = flag.get("pm_name", "")

    note_body = (
        f"OCA Flag [{flag_type.upper()}] — {urgency.upper()}\n"
        f"Client: {client_name}\n"
        f"PM: {pm_name}\n"
        f"Details: {details}"
    )
    create_note_on_deal(deal_id, note_body)


if __name__ == "__main__":
    run()
