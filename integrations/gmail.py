import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import base64
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from utils.logger import get_logger

logger = get_logger("integrations.gmail")


def _get_gmail_service(sender_email: str):
    """Build Gmail API service impersonating sender_email via domain-wide delegation."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_value = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_value:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    if sa_value.startswith("{"):
        sa_info = json.loads(sa_value)
    else:
        with open(sa_value) as f:
            sa_info = json.load(f)

    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://mail.google.com/"],
        subject=sender_email,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email(
    sender_email: str,
    to_email: str,
    subject: str,
    body_html: str,
    cc_email: str = "",
    retry: bool = True,
) -> bool:
    """Send an email via Gmail API with domain-wide delegation. Retries once on failure."""
    import time as _time

    def _attempt() -> bool:
        service = _get_gmail_service(sender_email)
        message = MIMEText(body_html, "html")
        message["to"] = to_email
        message["from"] = sender_email
        message["subject"] = subject
        if cc_email:
            message["cc"] = cc_email
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info(f"Email sent from {sender_email} to {to_email} (cc={cc_email}) id={result.get('id')}")
        return True

    try:
        return _attempt()
    except Exception as e:
        logger.warning(f"Gmail first attempt failed ({sender_email} → {to_email}): {e}")
        if retry:
            _time.sleep(30)
            try:
                return _attempt()
            except Exception as e2:
                logger.error(f"Gmail retry failed ({sender_email} → {to_email}): {e2}")
        return False


def _extract_body_text(payload: dict) -> str:
    """Walk a Gmail message payload to find the first text/plain part and decode it."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")
    for part in payload.get("parts", []) or []:
        text = _extract_body_text(part)
        if text:
            return text
    return ""


def get_pm_customer_email_history(
    pm_email: str,
    customer_email: str,
    since: "datetime | None" = None,
) -> "dict | None":
    """Search pm_email's Gmail sent folder for emails to customer_email.

    Impersonates pm_email via domain-wide delegation. If `since` is given (the
    prior fetched_at), searches from `since - 1 hour`; otherwise searches the
    last 90 days (first sync).

    Returns:
        {
            "last_sent_at": date,         # most recent sent date
            "last_sent_subject": str,      # subject of most recent email
            "email_snippets": str,         # last 3 email bodies joined, max 1500 chars
            "total_found": int             # total emails found in window
        }
    or None if Gmail fails for any reason, or no emails are found.
    """
    try:
        service = _get_gmail_service(pm_email)

        if since:
            window_start = since - timedelta(hours=1)
        else:
            window_start = datetime.now(timezone.utc) - timedelta(days=90)

        query = f"to:{customer_email} in:sent after:{window_start.strftime('%Y/%m/%d')}"

        list_result = service.users().messages().list(userId="me", q=query, maxResults=10).execute()
        message_ids = [m["id"] for m in list_result.get("messages", [])]
        total_found = len(message_ids)

        if not message_ids:
            logger.info(f"No sent emails found from {pm_email} to {customer_email}")
            return None

        messages = []
        for msg_id in message_ids:
            try:
                msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
                headers = msg.get("payload", {}).get("headers", [])
                subject = next((h["value"] for h in headers if h.get("name", "").lower() == "subject"), "")
                internal_date_ms = int(msg.get("internalDate", "0"))
                sent_date = datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc).date()
                body_text = _extract_body_text(msg.get("payload", {})) or msg.get("snippet", "")
                messages.append({
                    "internal_date_ms": internal_date_ms,
                    "date": sent_date,
                    "subject": subject,
                    "snippet": body_text[:500],
                })
            except Exception as e:
                logger.warning(f"Failed to fetch Gmail message {msg_id} for {pm_email}: {e}")
                continue

        if not messages:
            return None

        messages.sort(key=lambda m: m["internal_date_ms"], reverse=True)
        top3 = messages[:3]

        email_snippets = "\n---\n".join(m["snippet"] for m in top3)[:1500]

        return {
            "last_sent_at": top3[0]["date"],
            "last_sent_subject": top3[0]["subject"],
            "email_snippets": email_snippets,
            "total_found": total_found,
        }
    except Exception as e:
        logger.warning(f"Gmail history fetch failed ({pm_email} → {customer_email}): {e}")
        return None
