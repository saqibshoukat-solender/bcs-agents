import os
from typing import Any

from utils.logger import get_logger
from config.loader import cfg
from integrations.slack import send_dm, send_message

logger = get_logger("oca.alerts")

_JOSH_SLACK_USER_ID: str = os.getenv("JOSH_SLACK_USER_ID", "")


def normalize_pm_name(pm_name: str) -> str:
    """Normalize PM name for consistent matching; handles casing variants like FERNANDA."""
    name = pm_name.strip().title()
    if name.upper() == "FERNANDA":
        return "Fernanda"
    return name

_FLAG_TYPE_LABELS = {
    "stale_record":     "Stale Job Record",
    "missing_pm":       "Missing PM Assignment",
    "unconfirmed_crew": "Unconfirmed Crew/Sub",
    "dropped_invoice":  "Dropped Invoice Follow-up",
    "readiness_sync":   "HubSpot Sync Issue",
    "deal_not_found":   "Deal Not Found in HubSpot",
}

_URGENCY_EMOJI = {
    "urgent":  "🔴",
    "warning": "🟡",
    "info":    "🔵",
}

_URGENCY_RANK = {
    "urgent":  3,
    "warning": 2,
    "info":    1,
}


def _highest_urgency(job_flags: list[dict[str, Any]]) -> str:
    return max(
        (f.get("urgency", "info") for f in job_flags),
        key=lambda u: _URGENCY_RANK.get(u, 0),
        default="info",
    )


def _hubspot_deal_url(deal_id: str) -> str:
    portal_id = cfg("hubspot_portal_id") or "51566851"
    return f"https://app.hubspot.com/contacts/{portal_id}/deal/{deal_id}"


def build_alert_message(flag: dict[str, Any]) -> str:
    flag_type = flag.get("flag_type", "")
    label = _FLAG_TYPE_LABELS.get(flag_type, flag_type)
    deal_id = flag.get("hubspot_deal_id")
    deal_url = _hubspot_deal_url(deal_id) if deal_id else "—"

    lines = [
        f"🚨 *OCA Alert — {label}*",
        f"Customer: {flag.get('client_name', 'Unknown')}",
        f"PM: {flag.get('pm_name', 'Unassigned')}",
        f"Details: {flag.get('details', '')}",
    ]
    last_contact = flag.get("last_contact_date")
    if last_contact:
        lines.append(f"Last contact: {last_contact}")
    lines.append(f"HubSpot: {deal_url}")
    return "\n".join(lines)


def _dm(user_id: str, text: str, pm_name: str = "") -> None:
    send_dm(user_id, text, pm_name=pm_name)


def _post(channel: str, text: str) -> None:
    send_message(channel, text)


def _lookup_pm_slack_id(pm_name: str, pm_config: list) -> str:
    normalized = normalize_pm_name(pm_name)
    for pm in pm_config:
        if normalize_pm_name(pm.get("full_name", "")) == normalized:
            return pm.get("slack_user_id", "")
    return ""


def _route(
    message: str,
    urgency: str,
    pm_name: str,
    pm_config: list,
    josh_slack_id: str,
    sam_slack_id: str,
) -> None:
    pm_slack_id = _lookup_pm_slack_id(pm_name, pm_config)

    if urgency == "urgent":
        _dm(josh_slack_id, message)
        if pm_slack_id:
            _dm(pm_slack_id, message, pm_name=pm_name)
    elif urgency == "warning":
        if pm_slack_id:
            _dm(pm_slack_id, message, pm_name=pm_name)
        else:
            _dm(josh_slack_id, message)
    else:  # info
        oca_channel = os.getenv("SLACK_OCA_CHANNEL", "oca-alerts")
        _post(oca_channel, message)


def route_alert(
    flag: dict[str, Any],
    pm_config: list,
    josh_slack_id: str,
    sam_slack_id: str,
) -> None:
    message = build_alert_message(flag)
    urgency = flag.get("urgency", "info")
    pm_name = flag.get("pm_name", "")
    _route(message, urgency, pm_name, pm_config, josh_slack_id, sam_slack_id)


def build_combined_alert_message(job_flags: list[dict[str, Any]]) -> str:
    client_name = job_flags[0].get("client_name", "Unknown")
    pm_name = job_flags[0].get("pm_name", "Unassigned")

    lines = [
        f"🚨 *Multiple Issues — {client_name}*",
        f"PM: {pm_name}",
        "",
    ]
    for flag in job_flags:
        flag_type = flag.get("flag_type", "")
        label = _FLAG_TYPE_LABELS.get(flag_type, flag_type)
        urgency = flag.get("urgency", "info")
        emoji = _URGENCY_EMOJI.get(urgency, "🔵")
        lines.append(f"{emoji} {label}: {flag.get('details', '')}")

    overall = _highest_urgency(job_flags)
    lines.append("")
    lines.append(f"Overall urgency: {overall}")

    if overall == "urgent":
        deal_id = job_flags[0].get("hubspot_deal_id")
        if deal_id:
            lines.append(f"🔗 HubSpot: {_hubspot_deal_url(deal_id)}")

    return "\n".join(lines)


def route_combined_alert(
    job_flags: list[dict[str, Any]],
    pm_config: list,
    josh_slack_id: str,
    sam_slack_id: str,
) -> None:
    message = build_combined_alert_message(job_flags)
    urgency = _highest_urgency(job_flags)
    pm_name = job_flags[0].get("pm_name", "")
    _route(message, urgency, pm_name, pm_config, josh_slack_id, sam_slack_id)


def build_unresolved_warning_message(flag: dict[str, Any], hours_open: int) -> str:
    flag_type = flag.get("flag_type", "")
    label = _FLAG_TYPE_LABELS.get(flag_type, flag_type)
    client_name = flag.get("client_name", "Unknown")
    pm_name = flag.get("pm_name", "Unassigned")
    return (
        f"⚠️ Unresolved Warning — {label} for {client_name} has been open for "
        f"{hours_open} hours with no resolution. PM: {pm_name}."
    )


def escalate_unresolved_warning(
    flag: dict[str, Any],
    hours_open: int,
    josh_slack_id: str,
) -> None:
    message = build_unresolved_warning_message(flag, hours_open)
    _dm(josh_slack_id, message)


def build_deal_not_found_message(flag: dict[str, Any]) -> str:
    return (
        f"⚠️ *Deal Not Found in HubSpot*\n"
        f"Customer: {flag.get('client_name', 'Unknown')}\n"
        f"PM: {flag.get('pm_name', 'Unassigned')}\n"
        f"Reason: Job exists in sheet but no matching HubSpot deal found\n"
        f"Sheet email: {flag.get('customer_email') or 'Unknown'}\n"
        f"Action: Update customer email in sheet to match HubSpot contact, then re-run OCA"
    )


def send_deal_not_found_alert(
    flag: dict[str, Any],
    josh_slack_id: str,
) -> None:
    message = build_deal_not_found_message(flag)
    _dm(josh_slack_id, message)


def build_no_email_history_message(
    client_name: str,
    pm_name: str,
    pm_email: str,
    customer_email: str,
) -> str:
    return (
        f"⚠️ *No Email History Found*\n"
        f"Customer: {client_name}\n"
        f"PM: {pm_name}\n"
        f"Reason: No emails found in {pm_email} sent folder to {customer_email}\n"
        f"Action: Verify PM has contacted this customer"
    )


def send_no_email_history_alert(
    client_name: str,
    pm_name: str,
    pm_email: str,
    customer_email: str,
    josh_slack_id: str,
) -> None:
    message = build_no_email_history_message(client_name, pm_name, pm_email, customer_email)
    _dm(josh_slack_id, message)


def build_deadline_change_message(
    client_name: str,
    pm_name: str,
    old_deadline: str,
    new_deadline: str,
    deal_id: "str | None",
) -> str:
    deal_url = _hubspot_deal_url(deal_id) if deal_id else "—"
    return (
        f"📅 *Deadline to Start Changed*\n"
        f"Customer: {client_name}\n"
        f"PM: {pm_name or 'Unassigned'}\n"
        f"Previous deadline: {old_deadline}\n"
        f"New deadline: {new_deadline}\n"
        f"HubSpot: {deal_url}"
    )


def send_deadline_change_alert(
    client_name: str,
    pm_name: str,
    old_deadline: str,
    new_deadline: str,
    deal_id: "str | None",
    josh_slack_id: str,
) -> None:
    message = build_deadline_change_message(client_name, pm_name, old_deadline, new_deadline, deal_id)
    _dm(josh_slack_id, message)


def send_daily_digest(
    active_flags_summary: dict[str, int],
    sam_slack_id: str,
) -> None:
    stale = active_flags_summary.get("stale", 0)
    missing_pm = active_flags_summary.get("missing_pm", 0)
    unconfirmed = active_flags_summary.get("unconfirmed_crew", 0)
    dropped = active_flags_summary.get("dropped_invoice", 0)
    sync = active_flags_summary.get("readiness_sync", 0)

    urgent_count = stale + missing_pm + unconfirmed
    warning_count = dropped
    info_count = sync
    total = urgent_count + warning_count + info_count

    message = (
        f"📊 *OCA Daily Digest*\n"
        f"Active flags:\n"
        f"🔴 Urgent: {urgent_count}\n"
        f"🟡 Warnings: {warning_count}\n"
        f"🔵 Info: {info_count}\n"
        f"Total active issues: {total}"
    )
    recipient = sam_slack_id
    if not recipient:
        logger.warning("SAM_SLACK_USER_ID not set — sending daily digest to Josh instead")
        recipient = _JOSH_SLACK_USER_ID
    _dm(recipient, message)


def send_weekly_summary(
    weekly_data: dict[str, int],
    josh_slack_id: str,
) -> None:
    message = (
        f"📅 *OCA Weekly Summary*\n"
        f"Flags raised this week: {weekly_data.get('raised', 0)}\n"
        f"Flags resolved this week: {weekly_data.get('resolved', 0)}\n"
        f"Still active: {weekly_data.get('active', 0)}"
    )
    _dm(josh_slack_id, message)
