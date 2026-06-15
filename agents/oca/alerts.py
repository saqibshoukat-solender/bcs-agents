import os
from typing import Any

from utils.logger import get_logger
from config.loader import cfg

logger = get_logger("oca.alerts")

_JOSH_SLACK_USER_ID: str = os.getenv("JOSH_SLACK_USER_ID", "")

_FLAG_TYPE_LABELS = {
    "stale_record":     "Stale Job Record",
    "missing_pm":       "Missing PM Assignment",
    "unconfirmed_crew": "Unconfirmed Crew/Sub",
    "dropped_invoice":  "Dropped Invoice Follow-up",
    "readiness_sync":   "HubSpot Sync Issue",
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
    urgency = flag.get("urgency", "info")
    emoji = _URGENCY_EMOJI.get(urgency, "🔵")
    message = (
        f"🚨 *OCA Alert — {label}*\n"
        f"Customer: {flag.get('client_name', 'Unknown')}\n"
        f"PM: {flag.get('pm_name', 'Unassigned')}\n"
        f"Details: {flag.get('details', '')}\n"
        f"Urgency: {emoji} {urgency}"
    )
    if urgency == "urgent" and flag.get("hubspot_deal_id"):
        message += f"\n🔗 HubSpot: {_hubspot_deal_url(flag['hubspot_deal_id'])}"
    return message


def _dm(slack_client, user_id: str, text: str, pm_name: str = "") -> None:
    if not user_id:
        return
    try:
        response = slack_client.conversations_open(users=user_id)
        channel_id = response["channel"]["id"]
        slack_client.chat_postMessage(channel=channel_id, text=text)
        logger.info(f"DM sent to {user_id}")
    except Exception as e:
        error_str = str(e)
        if "user_not_found" in error_str:
            name = pm_name or user_id
            logger.warning(f"PM DM failed for {name} (user_not_found) — skipping PM DM")
        else:
            logger.error(f"Slack DM error to {user_id}: {e}")


def _post(slack_client, channel: str, text: str) -> None:
    try:
        slack_client.chat_postMessage(channel=channel, text=text)
        logger.info(f"Posted to #{channel}")
    except Exception as e:
        logger.error(f"Slack post error to {channel}: {e}")


def _lookup_pm_slack_id(pm_name: str, pm_config: list) -> str:
    for pm in pm_config:
        if pm.get("full_name") == pm_name:
            return pm.get("slack_user_id", "")
    return ""


def _route(
    message: str,
    urgency: str,
    pm_name: str,
    slack_client,
    pm_config: list,
    josh_slack_id: str,
    sam_slack_id: str,
) -> None:
    pm_slack_id = _lookup_pm_slack_id(pm_name, pm_config)

    if urgency == "urgent":
        _dm(slack_client, josh_slack_id, message)
        if pm_slack_id:
            _dm(slack_client, pm_slack_id, message, pm_name=pm_name)
    elif urgency == "warning":
        if pm_slack_id:
            _dm(slack_client, pm_slack_id, message, pm_name=pm_name)
        else:
            _dm(slack_client, josh_slack_id, message)
    else:  # info
        oca_channel = os.getenv("SLACK_OCA_CHANNEL", "oca-alerts")
        _post(slack_client, oca_channel, message)


def route_alert(
    flag: dict[str, Any],
    slack_client,
    pm_config: list,
    josh_slack_id: str,
    sam_slack_id: str,
) -> None:
    message = build_alert_message(flag)
    urgency = flag.get("urgency", "info")
    pm_name = flag.get("pm_name", "")
    _route(message, urgency, pm_name, slack_client, pm_config, josh_slack_id, sam_slack_id)


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
    slack_client,
    pm_config: list,
    josh_slack_id: str,
    sam_slack_id: str,
) -> None:
    message = build_combined_alert_message(job_flags)
    urgency = _highest_urgency(job_flags)
    pm_name = job_flags[0].get("pm_name", "")
    _route(message, urgency, pm_name, slack_client, pm_config, josh_slack_id, sam_slack_id)


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
    slack_client,
    josh_slack_id: str,
) -> None:
    message = build_unresolved_warning_message(flag, hours_open)
    _dm(slack_client, josh_slack_id, message)


def send_daily_digest(
    active_flags_summary: dict[str, int],
    slack_client,
    sam_slack_id: str,
    hs_sync_summary: "dict | None" = None,
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
    if hs_sync_summary:
        message += (
            f"\nHubSpot sync: {hs_sync_summary.get('matched_contact', 0)} matched by contact, "
            f"{hs_sync_summary.get('matched_name', 0)} matched by deal name, "
            f"{hs_sync_summary.get('created', 0)} created new, "
            f"{hs_sync_summary.get('not_linked', 0)} not linked (link-only)"
        )
    recipient = sam_slack_id
    if not recipient:
        logger.warning("SAM_SLACK_USER_ID not set — sending daily digest to Josh instead")
        recipient = _JOSH_SLACK_USER_ID
    _dm(slack_client, recipient, message)


def send_weekly_summary(
    weekly_data: dict[str, int],
    slack_client,
    josh_slack_id: str,
) -> None:
    message = (
        f"📅 *OCA Weekly Summary*\n"
        f"Flags raised this week: {weekly_data.get('raised', 0)}\n"
        f"Flags resolved this week: {weekly_data.get('resolved', 0)}\n"
        f"Still active: {weekly_data.get('active', 0)}"
    )
    _dm(slack_client, josh_slack_id, message)
