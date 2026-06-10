"""
QuickBooks Sandbox Setup Script
--------------------------------
1. Opens browser for OAuth — you click once to authorize
2. Captures the refresh token automatically
3. Creates test invoices matching BCS dummy sheet customers
4. Prints all tokens to save in .env

Usage:
    pip install requests
    python qb_setup.py
"""

import base64
import http.server
import json
import threading
import time
import urllib.parse
import webbrowser
from datetime import date, timedelta

import requests

# ── credentials ────────────────────────────────────────────────────────────────
CLIENT_ID     = "ABL2IrI2xNpBwZWwnSckIvLDMMywpfevXP8eI0QUjxpad3jNMM"
CLIENT_SECRET = "4TzYrpaqNlNvObQLjKc6YeuI3zTOOk8zaAQTsEP5"
REALM_ID      = "9341457224748811"
REDIRECT_URI  = "http://localhost:8080/callback"
SCOPE         = "com.intuit.quickbooks.accounting"

TOKEN_URL     = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
BASE_URL      = f"https://sandbox-quickbooks.api.intuit.com/v3/company/{REALM_ID}"
HEADERS       = {"Accept": "application/json", "Content-Type": "application/json"}

# ── globals set by OAuth callback ──────────────────────────────────────────────
auth_code   = None
access_token  = None
refresh_token = None

# ── Step 1: OAuth flow ─────────────────────────────────────────────────────────
class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Authorized! You can close this tab.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h2>No code received.</h2>")

    def log_message(self, *args):
        pass  # silence server logs

def get_tokens():
    global access_token, refresh_token

    auth_url = (
        "https://appcenter.intuit.com/connect/oauth2"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&response_type=code"
        f"&scope={urllib.parse.quote(SCOPE)}"
        f"&state=bcs_test"
    )

    server = http.server.HTTPServer(("localhost", 8080), CallbackHandler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()

    print("\n🌐  Opening browser for QuickBooks authorization...")
    print("    If it doesn't open, go to:\n")
    print(f"    {auth_url}\n")
    webbrowser.open(auth_url)

    thread.join(timeout=120)
    server.server_close()

    if not auth_code:
        raise RuntimeError("❌ No auth code received within 2 minutes.")

    print("✅  Auth code received — exchanging for tokens...")

    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": REDIRECT_URI,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    access_token  = data["access_token"]
    refresh_token = data["refresh_token"]
    print("✅  Tokens received!\n")

# ── Step 2: Helpers ─────────────────────────────────────────────────────────────
def qb_get(path):
    r = requests.get(
        f"{BASE_URL}/{path}",
        headers={**HEADERS, "Authorization": f"Bearer {access_token}"},
    )
    r.raise_for_status()
    return r.json()

def qb_post(path, payload):
    r = requests.post(
        f"{BASE_URL}/{path}",
        headers={**HEADERS, "Authorization": f"Bearer {access_token}"},
        json=payload,
    )
    if not r.ok:
        print(f"  ⚠️  POST {path} failed: {r.status_code} {r.text[:300]}")
        return None
    return r.json()

def find_or_create_customer(name, email):
    """Find existing customer or create new one."""
    q = urllib.parse.quote(f"SELECT * FROM Customer WHERE DisplayName = '{name}'")
    data = qb_get(f"query?query={q}")
    customers = data.get("QueryResponse", {}).get("Customer", [])
    if customers:
        return customers[0]["Id"]

    payload = {"DisplayName": name, "PrimaryEmailAddr": {"Address": email}}
    result = qb_post("customer", payload)
    if result:
        return result["Customer"]["Id"]
    return None

def find_service_item():
    """Get or create a generic service item for invoices."""
    q = urllib.parse.quote("SELECT * FROM Item WHERE Type = 'Service' MAXRESULTS 1")
    data = qb_get(f"query?query={q}")
    items = data.get("QueryResponse", {}).get("Item", [])
    if items:
        return items[0]["Id"]

    # Create one
    payload = {
        "Name": "Home Improvement Services",
        "Type": "Service",
        "IncomeAccountRef": {"value": "1", "name": "Services"},
    }
    result = qb_post("item", payload)
    if result:
        return result["Item"]["Id"]
    return "1"

def create_invoice(customer_id, item_id, amount, due_date, description):
    payload = {
        "CustomerRef": {"value": customer_id},
        "DueDate": str(due_date),
        "Line": [
            {
                "Amount": amount,
                "DetailType": "SalesItemLineDetail",
                "Description": description,
                "SalesItemLineDetail": {
                    "ItemRef": {"value": item_id},
                    "Qty": 1,
                    "UnitPrice": amount,
                },
            }
        ],
    }
    result = qb_post("invoice", payload)
    if result:
        return result["Invoice"]["Id"]
    return None

def mark_paid(invoice_id, amount, paid_date, customer_id):
    """Create a payment against the invoice."""
    payload = {
        "CustomerRef": {"value": customer_id},
        "TotalAmt": amount,
        "TxnDate": str(paid_date),
        "Line": [
            {
                "Amount": amount,
                "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}],
            }
        ],
    }
    qb_post("payment", payload)

# ── Step 3: Invoice scenarios ───────────────────────────────────────────────────
#
# Matches dummy sheet customers with 4 scenarios:
#   PAID        — invoice sent and paid (closed)
#   UNPAID      — invoice sent, not yet due (normal)
#   OVERDUE_30  — invoice sent 35 days ago, still unpaid
#   OVERDUE_60  — invoice sent 65 days ago, still unpaid (critical)
#
TODAY = date.today()

INVOICES = [
    # ── PAID invoices (deposit received, paid) ──
    {
        "customer": "Marcus Webb",
        "email": "mwebb@gmail.com",
        "amount": 7200.00,
        "description": "Deck Replacement — 50% Deposit",
        "due_days": -20,
        "scenario": "PAID",
        "paid_days_ago": 15,
    },
    {
        "customer": "Catherine Bloom",
        "email": "cbloom@hotmail.com",
        "amount": 9100.00,
        "description": "Bathroom Renovation — 50% Deposit",
        "due_days": -10,
        "scenario": "PAID",
        "paid_days_ago": 8,
    },
    {
        "customer": "Brenda Kowalski",
        "email": "bkowalski@yahoo.com",
        "amount": 6200.00,
        "description": "Landscaping + Sod — 50% Deposit",
        "due_days": -5,
        "scenario": "PAID",
        "paid_days_ago": 3,
    },

    # ── UNPAID — current, not yet due ──
    {
        "customer": "Trevor Nguyen",
        "email": "tnguyen@gmail.com",
        "amount": 5400.00,
        "description": "Fence + Gate — 50% Deposit Invoice",
        "due_days": 14,
        "scenario": "UNPAID",
    },
    {
        "customer": "Derrick Coleman",
        "email": "dcoleman@gmail.com",
        "amount": 4500.00,
        "description": "Concrete Patio — Progress Payment",
        "due_days": 10,
        "scenario": "UNPAID",
    },
    {
        "customer": "Frank Delgado",
        "email": "fdelgado@gmail.com",
        "amount": 5600.00,
        "description": "Retaining Wall — 50% Deposit Invoice",
        "due_days": 7,
        "scenario": "UNPAID",
    },

    # ── OVERDUE 30 days ──
    {
        "customer": "Helen Fitzgerald",
        "email": "hfitz@gmail.com",
        "amount": 11500.00,
        "description": "Roofing — Progress Payment #2",
        "due_days": -35,
        "scenario": "OVERDUE_30",
    },
    {
        "customer": "Omar Diallo",
        "email": "odiallo@gmail.com",
        "amount": 3800.00,
        "description": "Driveway Replacement — 50% Deposit",
        "due_days": -32,
        "scenario": "OVERDUE_30",
    },

    # ── OVERDUE 60 days (critical) ──
    {
        "customer": "Rachel Summers",
        "email": "rsummers@gmail.com",
        "amount": 4100.00,
        "description": "Basement Egress Window — Progress Payment",
        "due_days": -65,
        "scenario": "OVERDUE_60",
    },

    # ── To Start tab — deposit invoices sent, not yet due ──
    {
        "customer": "James Whitfield",
        "email": "jwhitfield@gmail.com",
        "amount": 6500.00,
        "description": "Fence Installation — 50% Deposit Invoice",
        "due_days": 21,
        "scenario": "UNPAID",
    },
    {
        "customer": "Patricia Donovan",
        "email": "pdonovan@hotmail.com",
        "amount": 8750.00,
        "description": "Deck Construction — 50% Deposit Invoice",
        "due_days": 18,
        "scenario": "UNPAID",
    },
    {
        "customer": "Stephanie Wu",
        "email": "swu@gmail.com",
        "amount": 18500.00,
        "description": "Full Interior Renovation — 50% Deposit",
        "due_days": -3,
        "scenario": "PAID",
        "paid_days_ago": 2,
    },
]

# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    get_tokens()

    print("🔍  Finding service item...")
    item_id = find_service_item()
    print(f"    Item ID: {item_id}\n")

    results = []

    for inv in INVOICES:
        name     = inv["customer"]
        email    = inv["email"]
        amount   = inv["amount"]
        desc     = inv["description"]
        scenario = inv["scenario"]
        due_date = TODAY + timedelta(days=inv["due_days"])

        print(f"  👤  {name} ({scenario}) — ${amount:,.2f}")

        cust_id = find_or_create_customer(name, email)
        if not cust_id:
            print(f"      ❌  Could not create customer, skipping")
            continue

        inv_id = create_invoice(cust_id, item_id, amount, due_date, desc)
        if not inv_id:
            print(f"      ❌  Could not create invoice, skipping")
            continue

        if scenario == "PAID":
            paid_date = TODAY - timedelta(days=inv.get("paid_days_ago", 5))
            mark_paid(inv_id, amount, paid_date, cust_id)
            print(f"      ✅  Invoice #{inv_id} created and marked PAID")
        else:
            days_overdue = -inv["due_days"] if inv["due_days"] < 0 else 0
            label = f"OVERDUE {days_overdue}d" if days_overdue > 0 else "UNPAID (current)"
            print(f"      ✅  Invoice #{inv_id} created — {label}")

        results.append({
            "customer": name,
            "invoice_id": inv_id,
            "customer_id": cust_id,
            "amount": amount,
            "scenario": scenario,
            "due_date": str(due_date),
        })

    print("\n" + "="*60)
    print("✅  ALL DONE\n")
    print("Add these to your .env:\n")
    print(f"  QB_CLIENT_ID={CLIENT_ID}")
    print(f"  QB_CLIENT_SECRET={CLIENT_SECRET}")
    print(f"  QB_REALM_ID={REALM_ID}")
    print(f"  QB_REFRESH_TOKEN={refresh_token}")
    print(f"  QB_ACCESS_TOKEN={access_token}")
    print(f"\n  (Access token expires in ~1hr — only refresh_token matters for production)\n")
    print("="*60)
    print("\nInvoices created:\n")
    for r in results:
        print(f"  {r['scenario']:12s}  ${r['amount']:>10,.2f}  {r['customer']}  (inv #{r['invoice_id']})")

if __name__ == "__main__":
    main()
