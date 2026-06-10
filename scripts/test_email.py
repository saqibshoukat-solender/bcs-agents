import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from agents.casey.email_composer import compose_customer_update_email
from integrations.gmail import send_email
from utils.logger import get_logger

logger = get_logger("test_email")

PM_EMAIL = "julie@bluecollarscholars.net"
PM_NAME = "Julie Martinez"
TO_EMAIL = "saqib.shoukat@solender.ai"
CUSTOMER_NAME = "Test Customer"
JOB_TYPE = "Deck Build and Landscaping"


def main():
    logger.info("=== Step 1: Composing email via Anthropic ===")
    email = compose_customer_update_email(
        customer_name=CUSTOMER_NAME,
        pm_name=PM_NAME,
        job_type=JOB_TYPE,
        start_date="June 15, 2026",
        contractor="Gary's Construction",
        notes="Permits approved, materials ordered",
    )
    logger.info(f"Subject: {email['subject']}")
    logger.info(f"Body preview: {email['body_html'][:200]}...")

    logger.info("=== Step 2: Sending email via Gmail API ===")
    success = send_email(
        sender_email=PM_EMAIL,
        to_email=TO_EMAIL,
        subject=email["subject"],
        body_html=email["body_html"],
    )

    if success:
        logger.info("SUCCESS — email sent! Check your inbox at saqib.shoukat@solender.ai")
    else:
        logger.error("FAILED — email was not sent. Check the error above.")
        logger.info(
            "If the error mentions 'delegation' or 'unauthorized', "
            "domain-wide delegation needs to be set up by the client's Google Workspace admin."
        )


if __name__ == "__main__":
    main()
