import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from utils.logger import get_logger
from config.loader import cfg

load_dotenv()
logger = get_logger("integrations.slack")

# Slack allows roughly one chat.postMessage per second per channel and will
# return a 429 ("ratelimited") once an app exceeds its burst allowance — easy
# to trip when a single OCA run fires alerts for many jobs/PMs back-to-back.
# A shared minimum-interval gate (module-level, lives for the process) spaces
# out every send from this process, and any "ratelimited" response is retried
# after the delay Slack actually asks for instead of being dropped.
_MIN_INTERVAL_SECONDS = 1.1
_MAX_ATTEMPTS = 5
_DEFAULT_RETRY_AFTER = 3.0

_send_lock = threading.Lock()
_last_sent_at = 0.0

_client_lock = threading.Lock()
_client_cache: "WebClient | None" = None
_client_token: "str | None" = None


def _client() -> WebClient:
    global _client_cache, _client_token
    token = cfg("slack_bot_token", "SLACK_BOT_TOKEN")
    with _client_lock:
        if _client_cache is None or _client_token != token:
            _client_cache = WebClient(token=token)
            _client_token = token
        return _client_cache


def _throttle_for_send() -> None:
    """Block just long enough that sends from this process stay >= _MIN_INTERVAL_SECONDS apart."""
    global _last_sent_at
    with _send_lock:
        wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _last_sent_at)
        if wait > 0:
            time.sleep(wait)
        _last_sent_at = time.monotonic()


def _retry_after_seconds(e: SlackApiError) -> float:
    try:
        header = e.response.headers.get("Retry-After")
        if header is not None:
            return max(float(header), 1.0)
    except Exception:
        pass
    return _DEFAULT_RETRY_AFTER


def _error_code(e: SlackApiError) -> str:
    try:
        return e.response.get("error") or str(e)
    except Exception:
        return str(e)


def _post_message(client: WebClient, channel: str, text: str, label: str) -> bool:
    """chat.postMessage, throttled + retried on 'ratelimited' until it succeeds or attempts run out."""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        _throttle_for_send()
        try:
            client.chat_postMessage(channel=channel, text=text)
            return True
        except SlackApiError as e:
            error = _error_code(e)
            if error == "ratelimited" and attempt < _MAX_ATTEMPTS:
                delay = _retry_after_seconds(e)
                logger.warning(
                    f"Slack ratelimited posting to {label} — retrying in {delay:.1f}s "
                    f"(attempt {attempt}/{_MAX_ATTEMPTS})"
                )
                time.sleep(delay)
                continue
            logger.error(f"Slack error posting to {label}: {error}")
            return False
    return False


def _open_dm_channel(client: WebClient, user_id: str) -> "tuple[str | None, str | None]":
    """conversations.open, retried on 'ratelimited' (no min-interval throttle — it doesn't post a message).

    Returns (channel_id, None) on success, or (None, error_code) on failure.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = client.conversations_open(users=user_id)
            return response["channel"]["id"], None
        except SlackApiError as e:
            error = _error_code(e)
            if error == "ratelimited" and attempt < _MAX_ATTEMPTS:
                delay = _retry_after_seconds(e)
                logger.warning(
                    f"Slack ratelimited opening DM with {user_id} — retrying in {delay:.1f}s "
                    f"(attempt {attempt}/{_MAX_ATTEMPTS})"
                )
                time.sleep(delay)
                continue
            return None, error
    return None, "ratelimited"


def send_message(channel: str, text: str) -> bool:
    ok = _post_message(_client(), channel, text, label=f"#{channel}")
    if ok:
        logger.info("Sent Slack message", extra={"channel": channel})
    return ok


def send_dm(user_id: str, text: str, pm_name: str = "") -> bool:
    if not user_id:
        return False
    client = _client()
    channel_id, error = _open_dm_channel(client, user_id)

    if not channel_id:
        if error and "user_not_found" in error:
            name = pm_name or user_id
            logger.warning(f"DM failed for {name} (user_not_found) — skipping")
        else:
            logger.error(f"Slack error opening DM to {user_id}: {error}")
        return False

    ok = _post_message(client, channel_id, text, label=f"DM:{pm_name or user_id}")
    if ok:
        logger.info("Sent Slack DM", extra={"user_id": user_id})
    return ok
