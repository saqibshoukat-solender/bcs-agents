import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base64
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import quote

import requests

from utils.logger import get_logger
from config.loader import cfg
from db.state_store import set_config

logger = get_logger("integrations.quickbooks")

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QB_SANDBOX = os.getenv("QUICKBOOKS_SANDBOX", "false").lower() == "true"
API_BASE = "https://sandbox-quickbooks.api.intuit.com" if QB_SANDBOX else "https://quickbooks.api.intuit.com"


def _save_tokens(token_data: dict, realm_id: str = "") -> None:
    """Persist access/refresh tokens and expiry to the DB.

    QuickBooks rotates the refresh token on every exchange/refresh — always
    store whatever comes back so the next refresh uses the latest one.
    """
    now = datetime.now(timezone.utc)
    expires_in = int(token_data.get("expires_in", 3600))
    set_config("qb_access_token", token_data["access_token"])
    if token_data.get("refresh_token"):
        set_config("qb_refresh_token", token_data["refresh_token"])
    set_config("qb_token_expiry", (now + timedelta(seconds=expires_in)).isoformat())
    if realm_id:
        set_config("qb_realm_id", realm_id)
    logger.info("QuickBooks tokens saved")


def exchange_code_for_tokens(code: str, redirect_uri: str, realm_id: str = "") -> bool:
    """Exchange an OAuth authorization code for access + refresh tokens (initial connect)."""
    client_id = cfg("qb_client_id")
    client_secret = cfg("qb_client_secret")
    if not client_id or not client_secret:
        logger.error("exchange_code_for_tokens: missing qb_client_id/qb_client_secret")
        return False
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        resp = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            timeout=15,
        )
        resp.raise_for_status()
        _save_tokens(resp.json(), realm_id=realm_id)
        return True
    except requests.RequestException as e:
        logger.error(f"QuickBooks token exchange failed: {e}")
        return False


def _refresh_access_token() -> "str | None":
    """Use the stored refresh token to get a new access token, saving the
    (rotated) refresh token and new expiry back to the DB."""
    client_id = cfg("qb_client_id")
    client_secret = cfg("qb_client_secret")
    refresh_token = cfg("qb_refresh_token")
    if not (client_id and client_secret and refresh_token):
        logger.warning("_refresh_access_token: QuickBooks is not connected")
        return None
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        resp = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _save_tokens(data)
        return data["access_token"]
    except requests.RequestException as e:
        logger.error(f"QuickBooks token refresh failed: {e}")
        return None


def _get_valid_access_token() -> "str | None":
    """Return a usable access token — refreshing it first if it's missing/near expiry."""
    access_token = cfg("qb_access_token")
    expires_at_str = cfg("qb_token_expiry")
    if access_token and expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if datetime.now(timezone.utc) < expires_at - timedelta(minutes=5):
                return access_token
        except ValueError:
            pass
    return _refresh_access_token()


def _qb_get(path: str) -> "dict | None":
    realm_id = cfg("qb_realm_id")
    access_token = _get_valid_access_token()
    if not (realm_id and access_token):
        logger.warning("_qb_get: QuickBooks not connected (missing realm_id or access token)")
        return None
    url = f"{API_BASE}/v3/company/{realm_id}/{path}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code in (401, 403):
            # Our local expiry said the token was still good, but QB rejected it
            # anyway (e.g. revoked/rotated early) — force a refresh and retry once.
            logger.warning(f"QuickBooks API {resp.status_code} on {path} — forcing token refresh and retrying")
            access_token = _refresh_access_token()
            if not access_token:
                return None
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                timeout=15,
            )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.error(f"QuickBooks API error ({path}): {e}")
        return None


def get_invoice_status_for_customer(customer_name: str) -> "dict | None":
    """
    Search QB invoices with an outstanding balance for this customer name
    (case-insensitive partial match on CustomerRef.name).

    Returns:
        {
            "found": bool,
            "status": "unpaid" | "overdue",
            "days_overdue": int,
            "amount_due": float,
            "invoice_id": str,
        }
        or None if QB is unavailable or the customer has no outstanding invoices.

    Never raises — any failure is logged and treated as "no QB data available"
    so a QB outage never blocks Casey's email send.
    """
    try:
        if not customer_name or not customer_name.strip():
            return None

        query = "SELECT * FROM Invoice WHERE Balance > '0'"
        data = _qb_get(f"query?query={quote(query)}")
        if not data:
            logger.warning(f"QB unavailable — skipping invoice check for {customer_name}")
            return None

        invoices = data.get("QueryResponse", {}).get("Invoice", [])
        if not invoices:
            return None

        needle = customer_name.strip().lower()
        today = datetime.now(timezone.utc).date()
        matches = []
        for inv in invoices:
            inv_customer = (inv.get("CustomerRef", {}).get("name") or "").strip().lower()
            if not inv_customer:
                continue
            if needle not in inv_customer and inv_customer not in needle:
                continue

            days_overdue = 0
            due_date_str = inv.get("DueDate", "")
            if due_date_str:
                try:
                    due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
                    days_overdue = max((today - due_date).days, 0)
                except ValueError:
                    pass

            matches.append({
                "found": True,
                "status": "overdue" if days_overdue > 0 else "unpaid",
                "days_overdue": days_overdue,
                "amount_due": float(inv.get("Balance", 0) or 0),
                "invoice_id": str(inv.get("Id", "")),
            })

        if not matches:
            return None

        matches.sort(key=lambda m: m["days_overdue"], reverse=True)
        return matches[0]

    except Exception as e:
        logger.warning(f"QB unavailable — skipping invoice check for {customer_name}: {e}")
        return None


def get_overdue_invoices() -> list[dict[str, Any]]:
    """Return invoices that are past their due date and still carry a balance.

    Used by Casey/OCA to flag clients with unpaid, overdue invoices.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    query = f"SELECT * FROM Invoice WHERE Balance > '0' AND DueDate < '{today}'"
    data = _qb_get(f"query?query={quote(query)}")
    if not data:
        return []

    invoices = data.get("QueryResponse", {}).get("Invoice", [])
    result = []
    for inv in invoices:
        result.append({
            "id": inv.get("Id"),
            "doc_number": inv.get("DocNumber"),
            "customer": inv.get("CustomerRef", {}).get("name", ""),
            "amount": inv.get("TotalAmt"),
            "balance": inv.get("Balance"),
            "due_date": inv.get("DueDate"),
        })
    return result
