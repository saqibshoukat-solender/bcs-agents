import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from utils.logger import get_logger
from config.loader import cfg

load_dotenv()
logger = get_logger("integrations.slack")


def _client() -> WebClient:
    return WebClient(token=cfg("slack_bot_token", "SLACK_BOT_TOKEN"))


def send_message(channel: str, text: str) -> bool:
    try:
        _client().chat_postMessage(channel=channel, text=text)
        logger.info("Sent Slack message", extra={"channel": channel})
        return True
    except SlackApiError as e:
        logger.error("Slack error sending message", extra={"channel": channel, "error": str(e)})
        return False


def send_dm(user_id: str, text: str) -> bool:
    try:
        response = _client().conversations_open(users=user_id)
        channel_id = response["channel"]["id"]
        _client().chat_postMessage(channel=channel_id, text=text)
        logger.info("Sent Slack DM", extra={"user_id": user_id})
        return True
    except SlackApiError as e:
        logger.error("Slack error sending DM", extra={"user_id": user_id, "error": str(e)})
        return False
