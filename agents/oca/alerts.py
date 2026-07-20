import os
from typing import Any

from utils.logger import get_logger
from config.loader import cfg
from integrations.slack import send_message

logger = get_logger("oca.alerts")


def normalize_pm_name(pm_name: str) -> str:
    """Normalize PM name for consistent matching; handles casing variants like FERNANDA."""
    name = pm_name.strip().title()
    if name.upper() == "FERNANDA":
        return "Fernanda"
    return name


_FLAG_TYPE_LABELS = {
    "stale_record":         "Stale Job Record",
    "missing_pm":           "Missing PM Assignment",
    "unconfirmed_crew":     "Unconfirmed Crew/Sub",
    "dropped_invoice":      "Dropped Invoice Follow-up",
    "readiness_sync":       "HubSpot Sync Issue",
    "deal_not_found":       "Deal Not Found in HubSpot",
    "approaching_deadline": "Approaching 7-Day Deadline",
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


def _get_urgentmatters_channel() -> str:
    return cfg("slack_urgentmatters_channel") or "urgentmatters"


def _route(message: str) -> None:
    send_message(_get_urgentmatters_channel(), message)


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


def route_alert(flag: dict[str, Any]) -> None:
    _route(build_alert_message(flag))


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


def route_combined_alert(job_flags: list[dict[str, Any]]) -> None:
    _route(build_combined_alert_message(job_flags))


def build_unresolved_warning_message(flag: dict[str, Any], hours_open: int) -> str:
    flag_type = flag.get("flag_type", "")
    label = _FLAG_TYPE_LABELS.get(flag_type, flag_type)
    client_name = flag.get("client_name", "Unknown")
    pm_name = flag.get("pm_name", "Unassigned")
    return (
        f"⚠️ Unresolved Warning — {label} for {client_name} has been open for "
        f"{hours_open} hours with no resolution. PM: {pm_name}."
    )


def escalate_unresolved_warning(flag: dict[str, Any], hours_open: int) -> None:
    _route(build_unresolved_warning_message(flag, hours_open))


def build_deal_not_found_message(flag: dict[str, Any]) -> str:
    return (
        f"⚠️ *Deal Not Found in HubSpot*\n"
        f"Customer: {flag.get('client_name', 'Unknown')}\n"
        f"PM: {flag.get('pm_name', 'Unassigned')}\n"
        f"Reason: Job exists in sheet but no matching HubSpot deal found\n"
        f"Sheet email: {flag.get('customer_email') or 'Unknown'}\n"
        f"Action: Update customer email in sheet to match HubSpot contact, then re-run OCA"
    )


def send_deal_not_found_alert(flag: dict[str, Any]) -> None:
    _route(build_deal_not_found_message(flag))


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
) -> None:
    _route(build_no_email_history_message(client_name, pm_name, pm_email, customer_email))


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
) -> None:
    _route(build_deadline_change_message(client_name, pm_name, old_deadline, new_deadline, deal_id))


def send_approaching_deadline_notifications(approaching_jobs: list[dict[str, Any]]) -> None:
    """Send one combined Slack message to #urgentmatters and one email to Chris for jobs at day 6."""
    if not approaching_jobs:
        return

    lines = ["⏰ *Approaching 7-Day PM Contact Deadline — Action Required*", ""]
    for job in approaching_jobs:
        client_name = job.get("client_name", "Unknown")
        pm_name = job.get("pm_name", "Unassigned")
        days = job.get("days_since_contact", 6)
        lines.append(f"• {client_name} (PM: {pm_name}) — {days} days since last contact")
    lines.append("")
    lines.append("These jobs will trigger Casey's backstop email tomorrow if the PM makes no contact today.")

    send_message(_get_urgentmatters_channel(), "\n".join(lines))

    chris_email = cfg("chris_notification_email") or ""
    sender_email = cfg("notification_sender_email") or ""
    if not chris_email:
        return
    if not sender_email:
        logger.warning("send_approaching_deadline_notifications: notification_sender_email not configured — skipping Chris email")
        return

    try:
        from integrations.gmail import send_email
        count = len(approaching_jobs)
        subject = f"Approaching Deadline — {count} job{'s' if count != 1 else ''} at day 6 of PM contact gap"
        body_html = "<br>".join(line.replace("*", "<b>", 1).replace("*", "</b>", 1) if "*" in line else line for line in lines)
        send_email(sender_email=sender_email, to_email=chris_email, subject=subject, body_html=f"<p>{body_html}</p>")
    except Exception as e:
        logger.warning(f"send_approaching_deadline_notifications: failed to email Chris: {e}")
