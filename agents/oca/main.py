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
    _flag,
)
from agents.oca.alerts import (
    route_alert,
    route_combined_alert,
    send_daily_digest,
    send_weekly_summary,
    escalate_unresolved_warning,
    send_deal_not_found_alert,
    send_deadline_change_alert,
    send_no_email_history_alert,
)
from db.state_store import (
    init_db,
    flag_exists,
    create_flag,
    should_alert_again,
    update_flag_alerted,
    get_flag_alert_age_hours,
    resolve_flag,
    resolve_flags_not_in,
    get_active_flags_summary,
    get_weekly_summary,
    is_first_run_today,
    upsert_active_job,
    set_config,
    get_config,
    set_hubspot_deal_id,
    set_hubspot_contact_id,
    get_email_history,
    upsert_email_history,
    should_fetch_email_history,
)
from integrations.slack import send_dm
from integrations.sheets import get_active_jobs, parse_latest_date
from integrations.gmail import get_pm_customer_email_history
from integrations.hubspot import (
    get_open_deals,
    hs_available,
    find_deal_for_job,
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
    "deal_not_found",
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


def _sync_all(sheet_jobs: list, hs_deals: list, slack_client, josh_slack_id: str) -> dict:
    """
    OCA sync: keep Google Sheet, HubSpot, and local DB aligned.

    A single customer can have multiple deals — one per job type — so deal
    resolution is keyed by (client_name, job_type), not client_name alone.

    Deal resolution order per job (stops at first hit), via find_deal_for_job():
      Layer 1 — DB: casey_active_jobs already has a hubspot_deal_id for this
                (client_name, job_type).
      Layer 2 — Contact match: find the customer's HubSpot contact (by email,
                then phone) and match one of their deals by job_type.
      Layer 3 — Deal-name search: search deals by client_name, filtered by
                job_type.

    OCA never creates HubSpot deals. If no deal is found after all layers, a
    deal_not_found flag is queued (returned to the caller) so Josh gets a
    condensed DM via the normal flag dedup/cooldown path.

    Returns a summary dict of HubSpot sync counters for the daily digest.
    """
    # Pre-load all DB jobs for Layer 1 lookup, keyed by (client_name, job_type) —
    # a customer can have multiple deals for different job types.
    from db.state_store import get_all_active_jobs
    db_map: dict[tuple[str, str], dict] = {
        (j["client_name"], j.get("job_type") or ""): j for j in get_all_active_jobs()
    }

    custom_field_map = _get_hs_custom_field_map()

    synced = 0
    created_contacts = 0
    matched_by_contact = 0
    matched_by_name = 0
    not_found = 0
    errors = 0
    deal_not_found_flags: list[dict] = []

    for job in sheet_jobs:
        client_name = job["client_name"]
        if not client_name.strip():
            continue

        pm_name = job.get("pm_name", "")
        job_type = job.get("job_type", "").strip()

        hubspot_deal_id: "str | None" = None
        hubspot_owner_name: str = ""
        hubspot_contact_id: "str | None" = None
        contact_needs_association = False
        old_deadline = ""

        # ── Deal Layer 1: DB check, keyed by (client_name, job_type) ─────────
        existing_db = db_map.get((client_name, job_type))
        if existing_db:
            hubspot_owner_name = existing_db.get("hubspot_owner_name") or ""
            if existing_db.get("hubspot_contact_id"):
                hubspot_contact_id = existing_db["hubspot_contact_id"]
            if existing_db.get("hubspot_deal_id"):
                hubspot_deal_id = existing_db["hubspot_deal_id"]
                logger.info(f"Using existing deal from DB for {client_name} / {job_type}: {hubspot_deal_id}")
            old_deadline = (existing_db.get("deadline_to_start") or "").strip()

        # ── Deal Layers 2-3: contact+job_type match, deal-name+job_type match ──
        if not hubspot_deal_id and hs_available():
            email = job.get("email", "").strip()
            phone = job.get("customer_phone", "").strip()
            found_id, layer = find_deal_for_job(client_name, job_type, email, phone, None)

            if found_id:
                hubspot_deal_id = found_id
                set_hubspot_deal_id(client_name, pm_name, hubspot_deal_id)
                contact_needs_association = True
                if layer == "contact":
                    matched_by_contact += 1
                elif layer == "name":
                    matched_by_name += 1
            else:
                not_found += 1
                flag = _flag(
                    client_name, pm_name, "deal_not_found",
                    "Job exists in sheet but no HubSpot deal found. Check that customer "
                    "email in sheet matches HubSpot contact email.",
                    "warning",
                )
                flag["customer_email"] = email
                deal_not_found_flags.append(flag)

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

        # ── Deadline to Start change detection ──────────────────────────────
        new_deadline = (job.get("deadline_to_start") or "").strip()
        if old_deadline and new_deadline and old_deadline != new_deadline:
            send_deadline_change_alert(
                client_name, pm_name, old_deadline, new_deadline,
                hubspot_deal_id, slack_client, josh_slack_id,
            )
            logger.info(f"Deadline change: {client_name} {old_deadline} → {new_deadline}")

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
                "most_recent_contact":    job.get("sheet_last_contact", ""),
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
                "deadline_to_start":      job.get("deadline_to_start", ""),
            })
            synced += 1
        except Exception as e:
            logger.error(f"OCA sync DB error for {client_name}: {e}")
            errors += 1

    set_config("last_sync_at", datetime.utcnow().isoformat())
    logger.info(
        f"OCA sync complete — sheet jobs: {len(sheet_jobs)}, "
        f"DB upserted: {synced}, HubSpot contacts created: {created_contacts}, "
        f"matched by contact: {matched_by_contact}, matched by deal name: {matched_by_name}, "
        f"not found: {not_found}, errors: {errors}"
    )
    return {
        "matched_contact": matched_by_contact,
        "matched_name": matched_by_name,
        "not_found": not_found,
        "deal_not_found_flags": deal_not_found_flags,
    }


def _fetch_email_histories(sheet_jobs: list, pm_config: list, slack_client, josh_slack_id: str) -> None:
    """
    Fetch and cache each job's PM→customer Gmail sent-history.

    Used by _build_contact_date_map() for staleness checks and by Casey for
    LLM context. Gmail failures (or zero emails ever found) never block the
    rest of the OCA run — at worst they fall back to the sheet's "Most Recent
    communication" column and/or a once-daily DM to Josh.
    """
    pm_email_map = {pm.get("full_name", ""): pm.get("email", "") for pm in pm_config}
    now = datetime.now(timezone.utc)

    for job in sheet_jobs:
        client_name = job["client_name"]
        if not client_name.strip():
            continue
        pm_name = job.get("pm_name", "")
        job_id = f"{client_name}|{pm_name}"
        customer_email = job.get("email", "").strip()

        if not should_fetch_email_history(client_name, pm_name):
            logger.info(f"Email history for {client_name} fetched within 23h — using cached value")
            continue

        pm_email = pm_email_map.get(pm_name, "")
        if not pm_email:
            logger.warning(f"Email history skip for {client_name} — PM '{pm_name}' has no email in pm_config")
            continue

        if not customer_email:
            logger.warning(f"Email history skip for {client_name} — no customer email on file")
            continue

        prior = get_email_history(client_name, pm_name)
        since = prior.fetched_at if prior else None

        result = get_pm_customer_email_history(pm_email, customer_email, since=since)

        if result:
            upsert_email_history(
                client_name, pm_name, customer_email,
                result["last_sent_at"], result["last_sent_subject"],
                result["email_snippets"], now,
            )
            resolve_flag(job_id, "no_email_history")
            logger.info(
                f"Email history fetched for {client_name}: "
                f"last sent {result['last_sent_at']}, {result['total_found']} emails found"
            )
        elif prior:
            logger.info(f"Gmail returned no new emails for {client_name}, using cached history")
            upsert_email_history(
                client_name, pm_name, prior.customer_email or customer_email,
                prior.last_sent_at, prior.last_sent_subject, prior.email_snippets, now,
            )
        else:
            logger.warning(f"No email history found for {client_name} — Gmail returned zero emails")
            flag_type = "no_email_history"
            if flag_exists(job_id, flag_type):
                if should_alert_again(job_id, flag_type, cooldown_hours=24):
                    update_flag_alerted(job_id, flag_type)
                    send_no_email_history_alert(client_name, pm_name, pm_email, customer_email, slack_client, josh_slack_id)
            else:
                create_flag(job_id, flag_type, f"No emails found in {pm_email} sent folder to {customer_email}", "warning")
                send_no_email_history_alert(client_name, pm_name, pm_email, customer_email, slack_client, josh_slack_id)


def _build_contact_date_map(sheet_jobs: list) -> dict:
    """Returns {client_name: last_contact_date | None}.

    Gmail-fetched history (cached by _fetch_email_histories) is the primary
    source; falls back to the sheet's "Most Recent communication" column.
    """
    contact_map: dict = {}
    for job in sheet_jobs:
        client_name = job["client_name"]
        pm_name = job.get("pm_name", "")

        history = get_email_history(client_name, pm_name)
        if history and history.last_sent_at:
            contact_map[client_name] = history.last_sent_at
            logger.info(f"Contact date for {client_name}: {history.last_sent_at} (source: gmail)")
            continue

        sheet_date = parse_latest_date(job.get("sheet_last_contact", ""))
        if sheet_date:
            contact_map[client_name] = sheet_date
            logger.info(f"Contact date for {client_name}: {sheet_date} (source: sheet fallback)")
            continue

        contact_map[client_name] = None
        logger.info(f"Contact date for {client_name}: none (no gmail or sheet data)")
    return contact_map


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
    hs_sync_summary = _sync_all(sheet_jobs, hs_deals, slack_client, josh_slack_id)

    # ── Step 2b: Gmail-based PM↔customer contact history ────────────────────
    _fetch_email_histories(sheet_jobs, pm_config, slack_client, josh_slack_id)
    contact_date_map = _build_contact_date_map(sheet_jobs)

    # Re-fetch open deals — check_job_readiness() needs the up-to-date list.
    hs_deals = get_open_deals()

    # Build a lookup of deal_id/contact_id for each client_name. _sync_all() writes
    # these synchronously, so this reflects deals linked this run even if
    # HubSpot's search index (hs_deals above) hasn't caught up yet.
    from db.state_store import get_all_active_jobs
    db_jobs = {j["client_name"]: j for j in get_all_active_jobs()}

    # ── Step 3: Run checks ───────────────────────────────────────────────────
    all_flags: list[dict] = []
    all_flags.extend(check_stale_records(sheet_jobs, contact_date_map))
    all_flags.extend(check_missing_pm(sheet_jobs, contact_date_map))
    all_flags.extend(check_unconfirmed_crew(sheet_jobs, contact_date_map))
    all_flags.extend(check_dropped_invoices(sheet_jobs, contact_date_map))
    all_flags.extend(check_job_readiness(sheet_jobs, hs_deals, db_jobs, contact_date_map))
    all_flags.extend(hs_sync_summary.get("deal_not_found_flags", []))

    logger.info(f"OCA detected {len(all_flags)} flags total")

    # ── Step 4: Process flags with deduplication + cooldown ─────────────────
    alerted = 0
    suppressed = 0

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

        # deal_not_found flags get a dedicated condensed DM to Josh — never
        # posted to #oca-alerts and never combined with other flag types.
        deal_not_found = [f for f in active_flags if f["flag_type"] == "deal_not_found"]
        other_flags = [f for f in active_flags if f["flag_type"] != "deal_not_found"]

        for flag in deal_not_found:
            send_deal_not_found_alert(flag, slack_client, josh_slack_id)

        if not other_flags:
            continue

        if len(other_flags) == 1:
            route_alert(other_flags[0], slack_client, pm_config, josh_slack_id, sam_slack_id)
        else:
            route_combined_alert(other_flags, slack_client, pm_config, josh_slack_id, sam_slack_id)

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
