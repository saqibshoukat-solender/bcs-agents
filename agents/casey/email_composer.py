import os
import json
import requests
from utils.logger import get_logger

logger = get_logger("casey.email_composer")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = """You are Casey, a warm and professional customer success assistant for Blue Collar Scholars,
a home improvement company based in the Washington DC area. You write update emails on behalf
of the assigned Project Manager to keep customers informed about their projects.

Rules you never break:
- Never mention specific dollar amounts anywhere in the email
- Never lead with or prominently feature payment or balance information
- For invoice_reminder scenario only: add ONE soft sentence in the final paragraph such as
  "As your project nears completion, we'll be in touch about final payment arrangements."
  Never say the amount. Never sound like collections.
- Emails are 3 to 4 short paragraphs, warm and professional
- Always open with something specific about the type of work being done
- Never make specific promises about dates unless a confirmed date exists in the job data
- Never use placeholder text like [X] or [date] — only include information you actually have
- Sign off with the PM's full name followed by a line break then "Blue Collar Scholars"
- Remove the 200 word limit — write naturally, but keep it concise"""

_SCENARIO_INSTRUCTIONS = {
    "not_started": (
        "This job has not started yet. The customer is waiting for their project to begin. "
        "Acknowledge that the project is scheduled and reassure them the team is preparing. "
        "Do NOT mention or reference any specific start date. "
        "Keep the tone warm and patient."
    ),
    "in_progress": (
        "The project is currently IN PROGRESS. Update the customer on the work being done. "
        "Mention the contractor / crew assigned if provided. Keep the tone positive and proactive."
    ),
    "invoice_reminder": (
        "Write this primarily as a positive project status update about the work being done. "
        "In the final paragraph only, add ONE soft sentence about being in touch regarding final "
        "payment arrangements as the project nears completion — per the system rules, never "
        "mention any amount and never sound like collections."
    ),
    "new_job_intro": (
        "This is the first email Casey sends after the deposit has been received and the "
        "project enters operations. Give the customer a warm welcome, confirm that their "
        "deposit was received, introduce the handoff from sales to the operations team, and "
        "set the expectation that their PM will be in touch within 2 weeks to discuss next steps. "
        "Do NOT mention or reference any specific start date. "
        "Sign off from the PM. Keep the email under 150 words."
    ),
}


def compose_customer_update_email(
    customer_name: str,
    pm_name: str,
    job_type: str,
    scenario: str = "in_progress",
    start_date: str = "",
    contractor: str = "",
    notes: str = "",
    to_collect: str = "",
    job_description: str = "",
    complaint_note: str = "",
    client_mood: str = "",
    total_project: str = "",
    estimator_name: str = "",
    sheet_tab: str = "",
    email_history: str = "",
) -> dict:
    """Use Anthropic API to compose a customer update email.
    scenario: 'not_started' | 'in_progress' | 'invoice_reminder' | 'new_job_intro'
    email_history: optional recent PM-to-customer email snippets for LLM context
    Returns {"subject": ..., "body_html": ...}
    """
    scenario_instruction = _SCENARIO_INSTRUCTIONS.get(scenario, _SCENARIO_INSTRUCTIONS["in_progress"])

    logger.info("Start date excluded from email context per client instruction")
    context_lines = []
    if job_type:
        context_lines.append(f"Job type: {job_type}")
    if job_description:
        context_lines.append(f"Job description: {job_description}")
    # start_date intentionally omitted from LLM context
    if contractor:
        context_lines.append(f"Assigned crew/contractor: {contractor}")
    if to_collect:
        context_lines.append("There is an outstanding balance on this project (do not state the amount).")
    if total_project:
        context_lines.append(f"Total project value: {total_project}")
    if estimator_name:
        context_lines.append(f"Estimator: {estimator_name}")
    if client_mood:
        context_lines.append(f"Client mood note: {client_mood}")
    if complaint_note:
        context_lines.append(f"Complaint on file: {complaint_note}")
    if notes:
        context_lines.append(f"Additional notes: {notes}")
    if sheet_tab == "to_start":
        context_lines.append("Sheet status: This job has not started yet.")

    context_block = "\n".join(context_lines) if context_lines else "No additional context."

    email_history_block = ""
    if email_history:
        email_history_block = f"""
Recent PM-to-customer email history (use this to write a natural follow-up,
reference prior conversations where relevant, do not repeat what was already said):
---
{email_history}
---
"""

    prompt = f"""Scenario: {scenario_instruction}

Customer name: {customer_name}
PM name (sender): {pm_name}
--- Job context ---
{context_block}
---
{email_history_block}
Address the customer by first name only.
Include one line encouraging them to reply with any questions.
Do NOT include a subject line in the body — it goes in a separate JSON field.

Return ONLY a JSON object with two keys:
{{"subject": "the email subject line", "body_html": "the email body as simple HTML with <p> tags"}}

Return ONLY the JSON, no markdown backticks, no other text."""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 1024,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        text = data["content"][0]["text"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        logger.info(f"Email composed [{scenario}] for {customer_name} by {pm_name}: subject='{result.get('subject','')}'")
    except Exception as e:
        response_body = ""
        try:
            response_body = e.response.text
        except Exception:
            pass
        logger.error(f"Anthropic email composition failed [{scenario}]: {e} — Response: {response_body}")
        first_name = customer_name.split()[0] if customer_name else customer_name
        if scenario == "invoice_reminder":
            body = (
                f"<p>Hi {first_name},</p>"
                f"<p>I hope your project is going well! As your project nears completion, we'll be in touch "
                f"about final payment arrangements. "
                f"Please don't hesitate to reach out with any questions.</p>"
                f"<p>Best regards,<br>{pm_name}<br>Blue Collar Scholars</p>"
            )
        elif scenario == "not_started":
            body = (
                f"<p>Hi {first_name},</p>"
                f"<p>Thank you for choosing Blue Collar Scholars! We wanted to reach out and confirm that your "
                f"project is scheduled and our team is preparing. We'll be in touch with more details soon. "
                f"Please reach out with any questions.</p>"
                f"<p>Best regards,<br>{pm_name}<br>Blue Collar Scholars</p>"
            )
        elif scenario == "new_job_intro":
            body = (
                f"<p>Hi {first_name},</p>"
                f"<p>Welcome to Blue Collar Scholars! We're happy to let you know that your deposit has been "
                f"received and your project is now moving into our operations phase.</p>"
                f"<p>Your Project Manager, {pm_name}, will be in touch within the next two weeks to walk you "
                f"through next steps.</p>"
                f"<p>Thank you for choosing us — please don't hesitate to reach out with any questions in "
                f"the meantime.</p>"
                f"<p>Best regards,<br>{pm_name}<br>Blue Collar Scholars</p>"
            )
        else:
            body = (
                f"<p>Hi {first_name},</p>"
                f"<p>This is a quick update on your project. We're making great progress and will keep you "
                f"informed every step of the way. Please don't hesitate to reach out with any questions.</p>"
                f"<p>Best regards,<br>{pm_name}<br>Blue Collar Scholars</p>"
            )
        result = {
            "subject": "Project Update — Blue Collar Scholars",
            "body_html": body,
        }

    # Label every Casey-sent email so OCA's Gmail history fetch can exclude
    # these automated updates and only see genuine PM-to-customer emails.
    result["subject"] = f"[BCS Update] {result.get('subject', '')}".strip()
    return result
