import json
import os
import secrets
from pathlib import Path
from urllib.parse import quote
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional

from db.state_store import (
    get_all_config, set_config,
    get_pm_list, add_pm, delete_pm,
    get_sales_rep_list, add_sales_rep, delete_sales_rep,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _google_service_account_email() -> str:
    """Read the client_email out of the configured Google service account JSON, if available."""
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw) if raw.startswith("{") else json.load(open(raw))
        return data.get("client_email", "")
    except Exception:
        return ""


def _toast(msg: str, success: bool = True) -> str:
    color = "emerald" if success else "red"
    icon  = "✓" if success else "✗"
    return f'<span class="inline-flex items-center gap-1.5 text-{color}-700 font-semibold"><span class="text-{color}-500">{icon}</span> {msg}</span>'


def _pm_table_html(pms: list) -> str:
    rows = ""
    for pm in pms:
        pid, name, email, slack = pm["id"], pm["full_name"], pm["email"], pm["slack_user_id"]
        rows += (
            f'<tr class="hover:bg-slate-50/80 transition-colors duration-100">'
            f'<td class="px-4 py-3 font-medium text-slate-800">{name}</td>'
            f'<td class="px-4 py-3 text-slate-600">{email}</td>'
            f'<td class="px-4 py-3 text-slate-500 font-mono text-xs">{slack}</td>'
            f'<td class="px-4 py-3 text-right">'
            f'<button hx-delete="/api/config/pm/{pid}" hx-target="#pm-tbody" hx-swap="innerHTML"'
            f' hx-confirm="Delete {name}?"'
            f' class="text-slate-400 hover:text-red-500 transition-colors duration-150 text-xs font-medium">Delete</button>'
            f'</td></tr>'
        )
    # Always re-include the hidden add row so it survives HTMX swaps
    rows += (
        '<tr id="pm-add-row" class="hidden bg-indigo-50/60">'
        '<td class="px-4 py-2"><input form="pm-add-form" name="full_name" type="text" placeholder="Full Name"'
        ' class="w-full px-2 py-1.5 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500"/></td>'
        '<td class="px-4 py-2"><input form="pm-add-form" name="email" type="email" placeholder="email@bcs.net"'
        ' class="w-full px-2 py-1.5 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500"/></td>'
        '<td class="px-4 py-2"><input form="pm-add-form" name="slack_user_id" type="text" placeholder="U0XXXXXXX"'
        ' class="w-full px-2 py-1.5 text-sm border border-slate-200 rounded-md font-mono focus:outline-none focus:ring-1 focus:ring-indigo-500"/></td>'
        '<td class="px-4 py-2 text-right whitespace-nowrap">'
        '<button type="submit" form="pm-add-form" class="px-2.5 py-1.5 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-md mr-1">Add</button>'
        '<button type="button" onclick="hideAddRow(\'pm\')" class="px-2.5 py-1.5 text-xs font-medium text-slate-600 bg-slate-200 hover:bg-slate-300 rounded-md">Cancel</button>'
        '</td></tr>'
    )
    return rows


def _sr_table_html(reps: list) -> str:
    rows = ""
    for r in reps:
        rid, name, email, owner = r["id"], r["name"], r["email"], r["hubspot_owner_id"]
        rows += (
            f'<tr class="hover:bg-slate-50/80 transition-colors duration-100">'
            f'<td class="px-4 py-3 font-medium text-slate-800">{name}</td>'
            f'<td class="px-4 py-3 text-slate-600">{email}</td>'
            f'<td class="px-4 py-3 text-slate-500 font-mono text-xs">{owner}</td>'
            f'<td class="px-4 py-3 text-right">'
            f'<button hx-delete="/api/config/salesrep/{rid}" hx-target="#sr-tbody" hx-swap="innerHTML"'
            f' hx-confirm="Delete {name}?"'
            f' class="text-slate-400 hover:text-red-500 transition-colors duration-150 text-xs font-medium">Delete</button>'
            f'</td></tr>'
        )
    # Always re-include the hidden add row so it survives HTMX swaps
    rows += (
        '<tr id="sr-add-row" class="hidden bg-purple-50/60">'
        '<td class="px-4 py-2"><input form="sr-add-form" name="name" type="text" placeholder="Full Name"'
        ' class="w-full px-2 py-1.5 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-1 focus:ring-purple-500"/></td>'
        '<td class="px-4 py-2"><input form="sr-add-form" name="email" type="email" placeholder="email@bcs.net"'
        ' class="w-full px-2 py-1.5 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-1 focus:ring-purple-500"/></td>'
        '<td class="px-4 py-2"><input form="sr-add-form" name="hubspot_owner_id" type="text" placeholder="owner_xxx"'
        ' class="w-full px-2 py-1.5 text-sm border border-slate-200 rounded-md font-mono focus:outline-none focus:ring-1 focus:ring-purple-500"/></td>'
        '<td class="px-4 py-2 text-right whitespace-nowrap">'
        '<button type="submit" form="sr-add-form" class="px-2.5 py-1.5 text-xs font-semibold text-white bg-purple-600 hover:bg-purple-700 rounded-md mr-1">Add</button>'
        '<button type="button" onclick="hideAddRow(\'sr\')" class="px-2.5 py-1.5 text-xs font-medium text-slate-600 bg-slate-200 hover:bg-slate-300 rounded-md">Cancel</button>'
        '</td></tr>'
    )
    return rows


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    db = get_all_config()
    pms  = get_pm_list()
    reps = get_sales_rep_list()
    qb_connected = bool(db.get("qb_refresh_token"))
    hs_connected = bool(db.get("hubspot_access_token"))
    return templates.TemplateResponse(request, "config.html", {
        "page": "config",
        "title": "Configurations",
        "pms": pms,
        "sales_reps": reps,
        "qb_connected": qb_connected,
        "hs_connected": hs_connected,
        "qb_redirect_uri": str(request.url_for("quickbooks_oauth_callback")),
        "google_sa_email": _google_service_account_email(),
        "cfg": db,   # all DB values — templates use cfg.key
    })


# ── Save endpoints ─────────────────────────────────────────────────────────────

@router.post("/api/config/quickbooks", response_class=HTMLResponse)
async def save_quickbooks(
    client_id:     Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None),
    realm_id:      Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
):
    if client_id:     set_config("qb_client_id",     client_id)
    if client_secret: set_config("qb_client_secret", client_secret)
    if realm_id:      set_config("qb_realm_id",      realm_id)
    if refresh_token: set_config("qb_refresh_token", refresh_token)
    return HTMLResponse(_toast("QuickBooks settings saved"))


# ── QuickBooks OAuth flow ──────────────────────────────────────────────────────
# 1. /connect  — client clicks "Connect QuickBooks" → redirected to Intuit's login
# 2. Intuit redirects back to /callback with an authorization code
# 3. We exchange that code for an access token + refresh token and save both to
#    the DB. From then on, integrations/quickbooks.py refreshes them automatically
#    whenever Casey/OCA need to call the QuickBooks API — the client never has to
#    reconnect.

QB_AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
QB_SCOPE = "com.intuit.quickbooks.accounting"


def _qb_oauth_page(message: str, success: bool) -> str:
    color = "#16a34a" if success else "#dc2626"
    icon = "✓" if success else "✗"
    return f"""<!DOCTYPE html>
<html><head><title>QuickBooks Connection</title></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; text-align:center; padding-top: 100px; background:#f8fafc;">
  <h2 style="color:{color};">{icon} {message}</h2>
  <p style="color:#64748b; font-size: 14px;">You can close this window — you'll be redirected back to the dashboard.</p>
  <script>setTimeout(function(){{ window.location = '/config#quickbooks'; }}, 2000);</script>
</body></html>"""


@router.get("/api/config/quickbooks/connect")
async def quickbooks_connect(request: Request):
    from db.state_store import get_config
    client_id = get_config("qb_client_id")
    if not client_id:
        return HTMLResponse(_qb_oauth_page(
            "Please save your Client ID, Client Secret, and Realm ID first, then click Connect QuickBooks again.",
            success=False,
        ))

    redirect_uri = str(request.url_for("quickbooks_oauth_callback"))
    state = secrets.token_urlsafe(16)
    set_config("qb_oauth_state", state)

    auth_url = (
        f"{QB_AUTHORIZE_URL}?client_id={quote(client_id)}"
        f"&redirect_uri={quote(redirect_uri)}"
        "&response_type=code"
        f"&scope={quote(QB_SCOPE)}"
        f"&state={state}"
    )
    return RedirectResponse(auth_url)


@router.get("/config/quickbooks/status", response_class=HTMLResponse)
async def quickbooks_status():
    """Status indicator for the config page: connected/expiry, or not connected.

    - green  "Connected — token valid until HH:MM UTC"  (refresh token + valid expiry in the future)
    - yellow "Connected — refreshing token"              (refresh token present, but no valid expiry yet)
    - red    "Not connected"                              (no refresh token saved)
    """
    from datetime import datetime, timezone
    from db.state_store import get_config

    refresh_token = get_config("qb_refresh_token")
    if not refresh_token:
        return HTMLResponse(
            '<span class="inline-flex items-center gap-1.5 text-xs font-medium text-red-700 bg-red-50 px-2.5 py-1 rounded-full ring-1 ring-red-100">'
            '<span class="w-1.5 h-1.5 bg-red-500 rounded-full"></span> Not connected</span>'
        )

    expires_at_str = get_config("qb_token_expiry")
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if datetime.now(timezone.utc) < expires_at:
                when = expires_at.strftime("%H:%M UTC")
                return HTMLResponse(
                    '<span class="inline-flex items-center gap-1.5 text-xs font-medium text-emerald-700 bg-emerald-50 px-2.5 py-1 rounded-full ring-1 ring-emerald-100">'
                    f'<span class="w-1.5 h-1.5 bg-emerald-500 rounded-full"></span> Connected — token valid until {when}</span>'
                )
        except ValueError:
            pass

    return HTMLResponse(
        '<span class="inline-flex items-center gap-1.5 text-xs font-medium text-amber-700 bg-amber-50 px-2.5 py-1 rounded-full ring-1 ring-amber-100">'
        '<span class="w-1.5 h-1.5 bg-amber-400 rounded-full animate-pulse"></span> Connected — refreshing token</span>'
    )


@router.get("/api/config/quickbooks/callback", name="quickbooks_oauth_callback")
async def quickbooks_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    realmId: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    from db.state_store import get_config
    from integrations.quickbooks import exchange_code_for_tokens

    if error:
        return HTMLResponse(_qb_oauth_page(f"QuickBooks authorization was cancelled ({error}).", success=False))
    if not code:
        return HTMLResponse(_qb_oauth_page("No authorization code was received from QuickBooks.", success=False))
    if not state or state != get_config("qb_oauth_state"):
        return HTMLResponse(_qb_oauth_page("Security check failed — please try connecting again.", success=False))

    redirect_uri = str(request.url_for("quickbooks_oauth_callback"))
    if exchange_code_for_tokens(code, redirect_uri, realm_id=realmId or ""):
        return HTMLResponse(_qb_oauth_page("QuickBooks connected successfully! Your tokens have been saved.", success=True))
    return HTMLResponse(_qb_oauth_page("Could not connect to QuickBooks — please double check your Client ID/Secret and try again.", success=False))


@router.post("/api/config/hubspot", response_class=HTMLResponse)
async def save_hubspot(
    access_token: Optional[str] = Form(None),
    pipeline_id:  Optional[str] = Form(None),
    portal_id:    Optional[str] = Form(None),
):
    if access_token: set_config("hubspot_access_token", access_token)
    if pipeline_id:  set_config("hubspot_pipeline_id",  pipeline_id)
    if portal_id:    set_config("hubspot_portal_id",    portal_id)
    return HTMLResponse(_toast("HubSpot settings saved"))


@router.post("/api/config/slack", response_class=HTMLResponse)
async def save_slack(
    bot_token:     Optional[str] = Form(None),
    josh_id:       Optional[str] = Form(None),
    sam_id:        Optional[str] = Form(None),
    oca_channel:   Optional[str] = Form(None),
    daily_channel: Optional[str] = Form(None),
):
    if bot_token:     set_config("slack_bot_token",     bot_token)
    if josh_id:       set_config("slack_josh_user_id",  josh_id)
    if sam_id:        set_config("slack_sam_user_id",   sam_id)
    if oca_channel:   set_config("slack_oca_channel",   oca_channel)
    if daily_channel: set_config("slack_casey_channel", daily_channel)
    return HTMLResponse(_toast("Slack settings saved"))


@router.post("/api/config/sheets", response_class=HTMLResponse)
async def save_sheets(sheets_id: Optional[str] = Form(None)):
    if sheets_id: set_config("google_sheets_id", sheets_id)
    return HTMLResponse(_toast("Google Sheets ID saved"))


@router.post("/api/config/hubspot/fields", response_class=HTMLResponse)
async def save_hubspot_fields(
    hubspot_field_pm_name:           Optional[str] = Form(None),
    hubspot_field_crew_confirmed:    Optional[str] = Form(None),
    hubspot_field_last_update_sent:  Optional[str] = Form(None),
    hubspot_field_next_update:       Optional[str] = Form(None),
    hubspot_field_escalation_flag:   Optional[str] = Form(None),
    hubspot_field_escalation_reason: Optional[str] = Form(None),
):
    fields = {
        "hubspot_field_pm_name":           hubspot_field_pm_name,
        "hubspot_field_crew_confirmed":    hubspot_field_crew_confirmed,
        "hubspot_field_last_update_sent":  hubspot_field_last_update_sent,
        "hubspot_field_next_update":       hubspot_field_next_update,
        "hubspot_field_escalation_flag":   hubspot_field_escalation_flag,
        "hubspot_field_escalation_reason": hubspot_field_escalation_reason,
    }
    for key, val in fields.items():
        if val is not None:
            set_config(key, val.strip())
    return HTMLResponse(_toast("HubSpot custom field names saved"))


@router.post("/api/config/hubspot/test", response_class=HTMLResponse)
async def test_hubspot():
    from db.state_store import get_config
    token = get_config("hubspot_access_token")
    if token:
        return HTMLResponse(_toast("Connected to HubSpot", success=True))
    return HTMLResponse(_toast("No access token configured", success=False))


# ── PM CRUD ────────────────────────────────────────────────────────────────────

@router.post("/api/config/pm/add", response_class=HTMLResponse)
async def api_add_pm(
    full_name:     Optional[str] = Form(None),
    email:         Optional[str] = Form(None),
    slack_user_id: Optional[str] = Form(None),
):
    if full_name:
        add_pm(full_name, email or "", slack_user_id or "")
    return HTMLResponse(_pm_table_html(get_pm_list()))


@router.delete("/api/config/pm/{pm_id}", response_class=HTMLResponse)
async def api_delete_pm(pm_id: int):
    delete_pm(pm_id)
    return HTMLResponse(_pm_table_html(get_pm_list()))


# ── Sales rep CRUD ─────────────────────────────────────────────────────────────

@router.post("/api/config/salesrep/add", response_class=HTMLResponse)
async def api_add_salesrep(
    name:             Optional[str] = Form(None),
    email:            Optional[str] = Form(None),
    hubspot_owner_id: Optional[str] = Form(None),
):
    if name:
        add_sales_rep(name, email or "", hubspot_owner_id or "")
    return HTMLResponse(_sr_table_html(get_sales_rep_list()))


@router.delete("/api/config/salesrep/{rep_id}", response_class=HTMLResponse)
async def api_delete_salesrep(rep_id: int):
    delete_sales_rep(rep_id)
    return HTMLResponse(_sr_table_html(get_sales_rep_list()))
