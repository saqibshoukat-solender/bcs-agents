import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import requests
from datetime import datetime, timezone, timedelta
from typing import Any
from dotenv import load_dotenv
from utils.logger import get_logger
from config.loader import cfg

load_dotenv()
logger = get_logger("integrations.hubspot")

CURRENT_PROJECTS_PIPELINE_ID = "default"
SALES_PIPELINE_ID = "default"

# Valid stages for the default (BCS Prospects) pipeline
ACTIVE_PROJECT_STAGE_IDS = [
    "1374503195",  # Stage 1
    "1374503518",  # Stage 2
    "1374503577",  # Stage 3 (Deposit Invoice Sent)
]

DEPOSIT_INVOICE_STAGE_ID = "1374503577"  # Deposit Invoice Sent (default pipeline)

DEAL_PROPERTIES = [
    "dealname",
    "dealstage",
    "pipeline",
    "hubspot_owner_id",
    "hs_lastmodifieddate",
    "notes_last_updated",
    "notes_last_contacted",
    "createdate",
    "closedate",
    "amount",
    "services_sold",
    "services_quoted",
]

# Module-level owner cache — fetched once per process
_owner_cache: dict[str, str] = {}

# Circuit breaker: set True on any 5xx response, blocks further writes for this run
_hs_circuit_open: bool = False
# Auth failure flag: set True on 401, blocks all HubSpot calls for this run
_hs_auth_failed: bool = False


def hs_available() -> bool:
    return not _hs_circuit_open and not _hs_auth_failed


def _trip_circuit(context: str) -> None:
    global _hs_circuit_open
    if not _hs_circuit_open:
        logger.error(f"HubSpot circuit breaker OPEN — skipping all further HS writes ({context})")
    _hs_circuit_open = True


def _trip_auth_failure() -> None:
    global _hs_auth_failed
    if not _hs_auth_failed:
        logger.error(
            "HubSpot authentication failed — skipping all HubSpot operations this run. "
            "Check token in dashboard config."
        )
    _hs_auth_failed = True


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg('hubspot_access_token', 'HUBSPOT_ACCESS_TOKEN')}"}


def _load_owners() -> None:
    global _owner_cache
    if _owner_cache:
        return
    url = "https://api.hubapi.com/crm/v3/owners?limit=100"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        if response.status_code == 401:
            _trip_auth_failure()
            return
        response.raise_for_status()
        for o in response.json().get("results", []):
            owner_id = str(o.get("id", ""))
            first = o.get("firstName") or ""
            last = o.get("lastName") or ""
            full_name = f"{first} {last}".strip() or o.get("email", owner_id)
            _owner_cache[owner_id] = full_name
            logger.info(f"Owner loaded: id={owner_id} name={full_name} email={o.get('email')}")
        logger.info(f"Owner cache built: {len(_owner_cache)} owners")
    except requests.RequestException as e:
        logger.error(f"HubSpot error loading owners: {e}")


def _search_deals(filter_groups: list[dict], label: str = "") -> list[dict[str, Any]]:
    url = "https://api.hubapi.com/crm/v3/objects/deals/search"
    headers = {**_headers(), "Content-Type": "application/json"}
    deals = []
    after = 0

    while True:
        payload = {
            "filterGroups": filter_groups,
            "properties": DEAL_PROPERTIES,
            "limit": 100,
            "after": after,
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=15)
            if response.status_code == 401:
                _trip_auth_failure()
                break
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.error(f"HubSpot search error ({label}): {e}")
            break

        results = data.get("results", [])
        logger.info(f"{label} page (after={after}): {len(results)} deals")

        for deal in results:
            deals.append({
                "id": deal.get("id"),
                "properties": deal.get("properties", {}),
            })

        next_after = data.get("paging", {}).get("next", {}).get("after")
        if next_after:
            after = next_after
        else:
            break

    return deals


def search_deal_by_exact_name(deal_name: str) -> "str | None":
    """Return the deal ID for the first deal whose dealname exactly matches deal_name, or None."""
    url = "https://api.hubapi.com/crm/v3/objects/deals/search"
    payload = {
        "filterGroups": [{"filters": [{
            "propertyName": "dealname",
            "operator": "EQ",
            "value": deal_name.strip(),
        }]}],
        "properties": ["dealname", "dealstage", "hubspot_owner_id"],
        "limit": 1,
    }
    try:
        resp = requests.post(url, headers={**_headers(), "Content-Type": "application/json"},
                             json=payload, timeout=10)
        if resp.status_code == 401:
            _trip_auth_failure()
            return None
        if resp.status_code >= 500:
            _trip_circuit(f"search_deal_by_exact_name {deal_name[:40]}")
            return None
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            did = results[0]["id"]
            logger.info(f"HubSpot exact search '{deal_name}' → id={did}")
            return did
        return None
    except requests.RequestException as e:
        logger.error(f"HubSpot search_deal_by_exact_name('{deal_name}'): {e}")
        return None


def get_all_deals_paginated(properties: "list[str] | None" = None) -> list[dict[str, Any]]:
    """Fetch every deal in the account via paginated list endpoint."""
    props = properties or ["dealname", "dealstage", "pipeline", "hubspot_owner_id", "createdate"]
    url = "https://api.hubapi.com/crm/v3/objects/deals"
    deals: list[dict] = []
    after: "str | None" = None
    while True:
        params: dict = {"limit": 100, "properties": ",".join(props)}
        if after:
            params["after"] = after
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=15)
            if resp.status_code == 401:
                _trip_auth_failure()
                break
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"HubSpot get_all_deals_paginated: {e}")
            break
        for d in data.get("results", []):
            deals.append({"id": d["id"], "properties": d.get("properties", {})})
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    logger.info(f"get_all_deals_paginated: fetched {len(deals)} deals total")
    return deals


def archive_deal(deal_id: str) -> bool:
    """Archive (soft-delete) a HubSpot deal by ID."""
    try:
        resp = requests.delete(
            f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}",
            headers=_headers(), timeout=10,
        )
        if resp.status_code == 204:
            return True
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"HubSpot archive_deal({deal_id}): {e}")
        return False


def get_open_deals() -> list[dict[str, Any]]:
    # --- Fetch 1: Deposit Invoice Sent (correct stage for default pipeline) ---
    deposit_deals = _search_deals(
        filter_groups=[{
            "filters": [
                {"propertyName": "dealstage", "operator": "EQ", "value": DEPOSIT_INVOICE_STAGE_ID},
            ]
        }],
        label="Deposit Invoice Sent",
    )
    logger.info(f"Deposit Invoice Sent: {len(deposit_deals)} deals")

    # --- Fetch 2: Closed Won in last 90 days ---
    cutoff_ms = str(int((datetime.now(timezone.utc) - timedelta(days=90)).timestamp() * 1000))
    closed_won_deals = _search_deals(
        filter_groups=[{
            "filters": [
                {"propertyName": "dealstage", "operator": "EQ", "value": "closedwon"},
                {"propertyName": "closedate", "operator": "GTE", "value": cutoff_ms},
            ]
        }],
        label="Closed Won (last 90 days)",
    )
    logger.info(f"Closed Won (last 90 days): {len(closed_won_deals)} deals")

    all_deals = deposit_deals + closed_won_deals
    logger.info(f"Total active deals: {len(all_deals)}")
    if not all_deals:
        logger.warning("No active deals found in either stage")
    return all_deals


def get_deals_from_sales_pipeline() -> list[dict[str, Any]]:
    """Deals in Deposit Invoice Sent stage — won, awaiting project start."""
    logger.info(f"Fetching sales pipeline deposit deals (stage={DEPOSIT_INVOICE_STAGE_ID})")
    deals = _search_deals(
        filter_groups=[{
            "filters": [
                {"propertyName": "dealstage", "operator": "EQ", "value": DEPOSIT_INVOICE_STAGE_ID},
            ]
        }],
        label="Deposit Invoice Sent (standalone)",
    )
    logger.info(f"Deposit Invoice Sent deals: {len(deals)}")
    return deals


def get_deal_contact(deal_id: str) -> dict[str, Any] | None:
    assoc_url = f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}/associations/contacts"
    try:
        assoc_resp = requests.get(assoc_url, headers=_headers(), timeout=10)
        if assoc_resp.status_code == 401:
            _trip_auth_failure()
            return None
        assoc_resp.raise_for_status()
        results = assoc_resp.json().get("results", [])
        if not results:
            logger.info(f"No contacts associated with deal {deal_id}")
            return None

        contact_id = results[0].get("id")
        contact_url = (
            f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
            "?properties=firstname,lastname,email,phone"
        )
        contact_resp = requests.get(contact_url, headers=_headers(), timeout=10)
        if contact_resp.status_code == 401:
            _trip_auth_failure()
            return None
        contact_resp.raise_for_status()
        props = contact_resp.json().get("properties", {})

        firstname = props.get("firstname") or ""
        lastname = props.get("lastname") or ""
        email = props.get("email") or ""

        if not firstname and not lastname:
            logger.warning(f"Contact {contact_id} has no name — using email prefix as fallback")
            firstname = email.split("@")[0] if email else ""

        return {
            "id": contact_id,
            "firstname": firstname,
            "lastname": lastname,
            "email": email,
            "phone": props.get("phone") or "",
        }

    except requests.RequestException as e:
        logger.error(f"HubSpot error fetching contact for deal {deal_id}: {e}")
        return None


def get_deal_stages() -> dict[str, str]:
    stage_map: dict[str, str] = {
        "1315907842": "Deposit Invoice Sent",
        "closedwon": "Closed Won",
    }

    for pipeline_id in [CURRENT_PROJECTS_PIPELINE_ID, SALES_PIPELINE_ID]:
        url = f"https://api.hubapi.com/crm/v3/pipelines/deals/{pipeline_id}"
        try:
            response = requests.get(url, headers=_headers(), timeout=10)
            if response.status_code == 401:
                _trip_auth_failure()
                break
            response.raise_for_status()
            for s in response.json().get("stages", []):
                stage_map[s["id"]] = s["label"]
            logger.info(f"Loaded stages from pipeline {pipeline_id}")
        except requests.RequestException as e:
            logger.error(f"HubSpot error fetching stages for pipeline {pipeline_id}: {e}")

    for sid, label in stage_map.items():
        logger.info(f"  Stage {sid}: {label}")
    return stage_map


def get_owner(owner_id: str) -> dict[str, Any] | None:
    _load_owners()
    name = _owner_cache.get(str(owner_id))
    if name:
        return {"id": owner_id, "full_name": name}
    logger.warning(f"Owner {owner_id} not found in cache")
    return None


def get_all_owners() -> dict[str, str]:
    """Returns owner_id -> full_name dict. Cached after first call."""
    _load_owners()
    return dict(_owner_cache)


def search_deals_by_client_name(client_name: str) -> list[dict[str, Any]]:
    url = "https://api.hubapi.com/crm/v3/objects/deals/search"
    headers = {**_headers(), "Content-Type": "application/json"}
    payload = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "dealname",
                "operator": "CONTAINS_TOKEN",
                "value": client_name,
            }]
        }],
        "properties": [
            "dealname", "dealstage", "pipeline", "hubspot_owner_id",
            "hs_lastmodifieddate", "notes_last_updated", "amount",
            "closedate", "services_sold",
        ],
        "limit": 10,
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code == 401:
            _trip_auth_failure()
            return []
        response.raise_for_status()
        results = response.json().get("results", [])
        deals = [{"id": d["id"], "properties": d.get("properties", {})} for d in results]
        logger.info(f"HubSpot search '{client_name}': {len(deals)} deals found")
        return deals
    except requests.RequestException as e:
        logger.error(f"HubSpot error searching deals for '{client_name}': {e}")
        return []


def create_deal(
    dealname: str,
    pipeline_id: str = "",
    stage_id: str = "",
    amount: str = "",
    owner_id: str = "",
) -> "str | None":
    """Create a HubSpot deal and return its ID, or None on failure."""
    if not hs_available():
        return None
    token = cfg("hubspot_access_token")
    if not token:
        logger.warning("create_deal: hubspot_access_token not configured")
        return None

    effective_pipeline = pipeline_id or CURRENT_PROJECTS_PIPELINE_ID
    # "New Jobs" is the first stage in the Current Projects pipeline.
    # Don't force a stage that belongs to a different pipeline — let HubSpot
    # use the pipeline's own default if no stage is provided.
    effective_stage = stage_id or "1374503195"  # First stage in default pipeline

    props: dict = {
        "dealname":  dealname,
        "pipeline":  effective_pipeline,
        "dealstage": effective_stage,
    }
    if amount:
        props["amount"] = amount
    if owner_id:
        props["hubspot_owner_id"] = owner_id
    try:
        resp = requests.post(
            "https://api.hubapi.com/crm/v3/objects/deals",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"properties": props},
            timeout=15,
        )
        if resp.status_code == 401:
            _trip_auth_failure()
            return None
        if resp.status_code >= 500:
            _trip_circuit(f"create_deal {dealname}")
            return None
        resp.raise_for_status()
        deal_id = resp.json().get("id")
        logger.info(f"Created HubSpot deal '{dealname}' → id={deal_id}")
        return deal_id
    except requests.RequestException as e:
        logger.error(f"HubSpot create_deal error for '{dealname}': {e}")
        return None


def search_contact_by_email(email: str) -> "str | None":
    """Return contact ID for the first contact matching email, or None."""
    if not email or not email.strip():
        return None
    url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email.strip()}]}],
        "properties": ["email", "firstname", "lastname"],
        "limit": 1,
    }
    try:
        resp = requests.post(url, headers={**_headers(), "Content-Type": "application/json"},
                             json=payload, timeout=10)
        if resp.status_code == 401:
            _trip_auth_failure()
            return None
        if resp.status_code >= 500:
            _trip_circuit(f"search_contact_by_email {email}")
            return None
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0]["id"] if results else None
    except requests.RequestException as e:
        logger.error(f"HubSpot search_contact_by_email({email}): {e}")
        return None


def search_contact_by_name(first_name: str, last_name: str = "") -> "str | None":
    """Return contact ID for first contact matching first+last name, or None."""
    filters = [{"propertyName": "firstname", "operator": "EQ", "value": first_name.strip()}]
    if last_name.strip():
        filters.append({"propertyName": "lastname", "operator": "EQ", "value": last_name.strip()})
    url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{"filters": filters}],
        "properties": ["email", "firstname", "lastname"],
        "limit": 1,
    }
    try:
        resp = requests.post(url, headers={**_headers(), "Content-Type": "application/json"},
                             json=payload, timeout=10)
        if resp.status_code == 401:
            _trip_auth_failure()
            return None
        if resp.status_code >= 500:
            _trip_circuit(f"search_contact_by_name {first_name}")
            return None
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0]["id"] if results else None
    except requests.RequestException as e:
        logger.error(f"HubSpot search_contact_by_name({first_name} {last_name}): {e}")
        return None


def search_contact_by_phone(phone_digits: str) -> "str | None":
    """Return contact ID for the first contact matching phone, or None.

    `phone_digits` must be digits only (no formatting). Tries the 'phone'
    property first, then falls back to 'mobilephone'.
    """
    if not phone_digits:
        return None
    url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    for prop in ("phone", "mobilephone"):
        payload = {
            "filterGroups": [{"filters": [{"propertyName": prop, "operator": "CONTAINS_TOKEN", "value": phone_digits}]}],
            "properties": ["email", "firstname", "lastname", "phone", "mobilephone"],
            "limit": 1,
        }
        try:
            resp = requests.post(url, headers={**_headers(), "Content-Type": "application/json"},
                                  json=payload, timeout=10)
            if resp.status_code == 401:
                _trip_auth_failure()
                return None
            if resp.status_code >= 500:
                _trip_circuit(f"search_contact_by_phone {phone_digits}")
                return None
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if results:
                return results[0]["id"]
        except requests.RequestException as e:
            logger.error(f"HubSpot search_contact_by_phone({phone_digits}, {prop}): {e}")
    return None


def get_deals_for_contact(contact_id: str) -> list[dict[str, Any]]:
    """Return deal objects ({"id", "properties": {"dealname": ...}}) associated with a contact."""
    if not contact_id:
        return []
    try:
        resp = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}/associations/deals",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code == 401:
            _trip_auth_failure()
            return []
        if resp.status_code >= 500:
            _trip_circuit(f"get_deals_for_contact {contact_id}")
            return []
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except requests.RequestException as e:
        logger.error(f"HubSpot get_deals_for_contact({contact_id}) associations: {e}")
        return []

    deals = []
    for r in results:
        deal_id = r.get("id") or r.get("toObjectId")
        if not deal_id:
            continue
        try:
            deal_resp = requests.get(
                f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}",
                headers=_headers(),
                params={"properties": "dealname,dealstage"},
                timeout=10,
            )
            if deal_resp.status_code == 401:
                _trip_auth_failure()
                return deals
            deal_resp.raise_for_status()
            data = deal_resp.json()
            deals.append({"id": str(data.get("id")), "properties": data.get("properties", {})})
        except requests.RequestException as e:
            logger.error(f"HubSpot get_deals_for_contact: failed to fetch deal {deal_id}: {e}")
    return deals


def find_deal_for_job(
    client_name: str,
    job_type: str,
    email: str,
    phone: str,
    db_deal_id: "str | None",
) -> "tuple[str | None, str]":
    """Resolve the HubSpot deal for one job (a client + job_type combination).

    A single customer can have multiple deals — one per job type — so a plain
    client-name match is not enough to pick the right deal. Returns
    (deal_id, layer) where layer is one of:
      "db"      — deal_id came from the local DB (already linked)
      "contact" — matched via the customer's HubSpot contact + job_type
      "name"    — matched via deal-name search on client_name + job_type
      "none"    — no match found; caller decides whether to create or skip
    """
    # Layer 1: already linked in the local DB
    if db_deal_id:
        return db_deal_id, "db"

    contact_id = None

    # Layer 2a: find contact by email
    if email:
        contact_id = search_contact_by_email(email)

    # Layer 2b: find contact by phone if email lookup failed
    if not contact_id and phone:
        clean_phone = re.sub(r"\D", "", phone)
        if clean_phone:
            contact_id = search_contact_by_phone(clean_phone)

    # Layer 2c: if a contact was found, match one of their deals. Any deal the
    # contact is genuinely associated with should be used — job_type match is
    # a preference for picking the right one among several, not a hard
    # requirement. Falling through to Layer 3 only happens when the contact
    # has zero deals at all.
    if contact_id:
        deals = get_deals_for_contact(contact_id)
        if deals:
            # Priority 1: exact job type match
            job_type_match = None
            if job_type:
                job_type_key = job_type.lower()[:15]
                for deal in deals:
                    deal_name = deal.get("properties", {}).get("dealname", "")
                    if job_type_key in deal_name.lower():
                        job_type_match = deal
                        break

            if job_type_match:
                logger.info(f"Matched deal by contact+job_type for {client_name}: {job_type_match['id']}")
                return job_type_match["id"], "contact"

            # Priority 2: exactly one deal associated with this contact
            if len(deals) == 1:
                logger.info(f"Single deal for contact {client_name}, using it directly: {deals[0]['id']}")
                return deals[0]["id"], "contact"

            # Priority 3: deal name contains the client's first or last name
            name_parts = client_name.strip().split(None, 1)
            first_name = name_parts[0].lower() if name_parts else ""
            last_name = name_parts[1].lower() if len(name_parts) > 1 else ""
            for deal in deals:
                deal_name = deal.get("properties", {}).get("dealname", "").lower()
                if (first_name and first_name in deal_name) or (last_name and last_name in deal_name):
                    logger.info(f"Matched deal by client name for {client_name}: {deal['id']}")
                    return deal["id"], "contact"

            # Priority 4: most recently created deal (highest deal ID)
            most_recent = max(deals, key=lambda d: int(d["id"]))
            logger.info(f"No name match for {client_name}, using most recent deal: {most_recent['id']}")
            return most_recent["id"], "contact"

        logger.warning(f"Contact found but no deal matched for {client_name} — falling through to name search")

    # Layer 3: deal-name search by client name, filtered by job type
    deals = search_deals_by_client_name(client_name)
    for deal in deals:
        deal_name = deal.get("properties", {}).get("dealname", "")
        if job_type and job_type.lower()[:15] in deal_name.lower():
            logger.info(f"Matched deal by name+job_type for {client_name}: {deal['id']}")
            return deal["id"], "name"

    # Layer 4: no match — caller decides whether to create or skip
    return None, "none"


def create_contact(firstname: str, lastname: str = "", email: str = "", phone: str = "") -> "str | None":
    """Create a HubSpot contact and return its ID, or None on failure."""
    if not hs_available():
        return None
    props: dict = {"firstname": firstname}
    if lastname:
        props["lastname"] = lastname
    if email:
        props["email"] = email
    if phone:
        props["phone"] = phone
    try:
        resp = requests.post(
            "https://api.hubapi.com/crm/v3/objects/contacts",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"properties": props},
            timeout=10,
        )
        if resp.status_code == 401:
            _trip_auth_failure()
            return None
        if resp.status_code == 409:
            # Duplicate — extract existing contact ID from error body
            existing_id = resp.json().get("message", "").split("Existing ID: ")
            if len(existing_id) > 1:
                return existing_id[1].strip()
            return None
        if resp.status_code >= 500:
            _trip_circuit(f"create_contact {firstname}")
            return None
        resp.raise_for_status()
        contact_id = resp.json().get("id")
        logger.info(f"Created HubSpot contact '{firstname} {lastname}' → id={contact_id}")
        return contact_id
    except requests.RequestException as e:
        logger.error(f"HubSpot create_contact({firstname}): {e}")
        return None


def associate_contact_to_deal(contact_id: str, deal_id: str) -> bool:
    """Associate an existing contact to a deal."""
    if not hs_available() or not contact_id or not deal_id:
        return False
    url = (
        f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}"
        f"/associations/contacts/{contact_id}/deal_to_contact"
    )
    try:
        resp = requests.put(url, headers=_headers(), timeout=10)
        if resp.status_code == 401:
            _trip_auth_failure()
            return False
        if resp.status_code >= 500:
            _trip_circuit(f"associate_contact deal={deal_id} contact={contact_id}")
            return False
        if resp.status_code in (200, 201, 204):
            logger.info(f"Associated contact {contact_id} to deal {deal_id}")
            return True
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"HubSpot associate_contact_to_deal: {e}")
        return False


def update_deal_properties(deal_id: str, properties: dict) -> bool:
    """Patch arbitrary properties onto a HubSpot deal."""
    if not hs_available() or not deal_id or not properties:
        return False
    try:
        resp = requests.patch(
            f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"properties": properties},
            timeout=10,
        )
        if resp.status_code == 401:
            _trip_auth_failure()
            return False
        if resp.status_code == 400:
            # Property names don't exist in HubSpot yet — warn, don't error
            try:
                detail = resp.json().get("message", resp.text[:200])
            except Exception:
                detail = resp.text[:200]
            logger.warning(
                f"HubSpot update_deal_properties({deal_id}): 400 — one or more properties not found. "
                f"Create them in HubSpot → Settings → Properties → Deals. Detail: {detail}"
            )
            return False
        if resp.status_code >= 500:
            _trip_circuit(f"update_deal_properties deal={deal_id}")
            return False
        resp.raise_for_status()
        logger.info(f"Updated deal {deal_id} properties: {list(properties.keys())}")
        return True
    except requests.RequestException as e:
        logger.error(f"HubSpot update_deal_properties({deal_id}): {e}")
        return False


def create_note_on_deal(deal_id: str, note_body: str) -> "str | None":
    """Create a CRM note associated with a deal. Returns note ID or None."""
    if not hs_available() or not deal_id or not note_body:
        return None
    import time as _time
    ts_ms = str(int(_time.time() * 1000))
    payload = {
        "properties": {
            "hs_note_body": note_body,
            "hs_timestamp": ts_ms,
        },
        "associations": [{
            "to": {"id": deal_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}],
        }],
    }
    try:
        resp = requests.post(
            "https://api.hubapi.com/crm/v3/objects/notes",
            headers={**_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        if resp.status_code == 401:
            _trip_auth_failure()
            return None
        if resp.status_code >= 500:
            _trip_circuit(f"create_note_on_deal deal={deal_id}")
            return None
        resp.raise_for_status()
        note_id = resp.json().get("id")
        logger.info(f"Created note on deal {deal_id} → note_id={note_id}")
        return note_id
    except requests.RequestException as e:
        logger.error(f"HubSpot create_note_on_deal({deal_id}): {e}")
        return None


def find_or_create_contact(client_name: str, email: str = "", phone: str = "") -> "str | None":
    """Search for contact by email, then by name; create if not found."""
    if not hs_available():
        return None
    # 1) Try email lookup
    if email and email.strip():
        cid = search_contact_by_email(email.strip())
        if cid:
            logger.info(f"Found contact by email {email} → id={cid}")
            return cid
    # 2) Try name lookup
    parts = client_name.strip().split(None, 1)
    first = parts[0] if parts else client_name
    last = parts[1] if len(parts) > 1 else ""
    cid = search_contact_by_name(first, last)
    if cid:
        logger.info(f"Found contact by name '{client_name}' → id={cid}")
        return cid
    # 3) Create new contact
    return create_contact(first, last, email, phone)


def get_contact_email_for_deal(deal_id: str) -> str | None:
    assoc_url = f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}/associations/contacts"
    try:
        assoc_resp = requests.get(assoc_url, headers=_headers(), timeout=10)
        if assoc_resp.status_code == 401:
            _trip_auth_failure()
            return None
        assoc_resp.raise_for_status()
        results = assoc_resp.json().get("results", [])
        if not results:
            return None
        contact_id = results[0].get("id")
        contact_url = (
            f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
            "?properties=email"
        )
        contact_resp = requests.get(contact_url, headers=_headers(), timeout=10)
        if contact_resp.status_code == 401:
            _trip_auth_failure()
            return None
        contact_resp.raise_for_status()
        return contact_resp.json().get("properties", {}).get("email") or None
    except requests.RequestException as e:
        logger.error(f"HubSpot error fetching email for deal {deal_id}: {e}")
        return None
