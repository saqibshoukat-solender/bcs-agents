from typing import Any
from utils.logger import get_logger

logger = get_logger("casey.escalation")

_COMPLETE_STAGES = {"Project Finished", "Project complete"}


def should_escalate(ticket: dict[str, Any], days_since_last_update: int) -> tuple[bool, str]:
    stage_label = ticket.get("stage_label") or ticket.get("properties", {}).get("hs_pipeline_stage", "")

    if days_since_last_update >= 14:
        return (True, "No customer update in 14+ days")

    if stage_label in _COMPLETE_STAGES and days_since_last_update >= 7:
        return (True, "Job marked complete but no recent activity")

    if stage_label == "New - Not Contacted" and days_since_last_update >= 7:
        return (True, "Job sold but never contacted after 7 days")

    return (False, "")


def build_escalation_slack_message(
    ticket: dict[str, Any],
    contact: dict[str, Any] | None,
    reason: str,
    days: int,
) -> str:
    props = ticket.get("properties", {})
    stage_label = ticket.get("stage_label") or props.get("hs_pipeline_stage", "unknown")
    job_type = props.get("dealname") or props.get("subject") or "Unknown job"
    pm_name = ticket.get("pm", "Unknown PM")

    if contact:
        customer_name = f"{contact.get('firstname', '')} {contact.get('lastname', '')}".strip()
    else:
        customer_name = "Unknown Customer"

    return (
        f"⚠️ Escalation: {customer_name} — {job_type}\n"
        f"Reason: {reason}\n"
        f"Stage: {stage_label}\n"
        f"Last update: {days} days ago\n"
        f"PM Pipeline: {pm_name}"
    )
