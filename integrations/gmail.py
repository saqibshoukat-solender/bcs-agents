import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import base64
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
