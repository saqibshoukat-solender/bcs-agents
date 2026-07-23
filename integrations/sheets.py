import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from typing import Any
from dotenv import load_dotenv
from utils.logger import get_logger
from config.loader import cfg

load_dotenv()
logger = get_logger("integrations.sheets")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _load_sa_info() -> dict:
    import os as _os
    value = _os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not value:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    if value.startswith("{"):
        return json.loads(value)
    with open(value) as f:
        return json.load(f)


def _get_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_info = _load_sa_info()
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_all_rows(
    spreadsheet_id: str,
    sheet_name: str,
    header_row: int = 1,
) -> list[dict[str, Any]]:
    """
    Fetch all rows from a sheet tab.
    header_row: 1-based index of the row containing column headers.
    All rows before it are skipped; data rows start immediately after.
    """
    if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip():
        logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set — skipping Sheets fetch")
        return []

    try:
        service = _get_service()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=sheet_name)
            .execute()
        )
        values = result.get("values", [])
        if not values or len(values) < header_row:
            logger.info(f"Sheet '{sheet_name}' returned no data")
            return []

        headers = values[header_row - 1]          # 0-indexed
        data_rows = values[header_row:]            # everything after header row

        rows = [
            dict(zip(headers, row + [""] * (len(headers) - len(row))))
            for row in data_rows
        ]
        logger.info(f"Fetched {len(rows)} rows from sheet '{sheet_name}' (header_row={header_row})")
        return rows

    except ValueError as e:
        logger.warning(f"Sheets config error: {e}")
        return []
    except Exception as e:
        logger.error(f"Sheets API error for '{sheet_name}': {e}")
        return []


PM_NAME_MAP = {
    "lau":       "Laura Peña",
    "laura":     "Laura Peña",
    "laura a":   "Laura Arbelaez",
    "laura.a":   "Laura Arbelaez",
    "julie":     "Julie Martinez",
    "tatiana":   "Tatiana Moreno",
    "tati":      "Tatiana Moreno",
    "gustavo":   "Gustavo Zuluaga",
    "tavo":      "Gustavo Zuluaga",
    "dan":       "Dan Diazgranados",
    "daniel":    "Daniel Gomez Cortez",
    "alfredo":   "Alfredo Núñez",
    "alfred":    "Alfredo Núñez",
    "esteban":   "Esteban Estarita",
    "steve":     "Esteban Estarita",
    "andrea":    "Andrea Ortega",
    "santi":     "Santiago",
    "jordan":    "Jordan Hantman",
    "ryan":      "Ryan Annis",
    "christian": "Christian Audé",
    "karen":     "Karen Garrido",
    "elias":     "Elias Babilonia",
    "lucia":     "Lucia Fuente Buena",
    "yower":     "Yower",
}


def normalize_pm_name(raw_name: str) -> str:
    key = raw_name.strip().lower()
    return PM_NAME_MAP.get(key, raw_name.strip())


def parse_latest_date(cell_value: str) -> "date | None":
    from datetime import date, datetime
    if not cell_value or not cell_value.strip():
        return None

    FORMATS = ["%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y", "%m/%d", "%m-%d"]
    current_year = date.today().year
    best: date | None = None

    for raw in cell_value.replace("\r", "\n").split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        for fmt in FORMATS:
            try:
                parsed = datetime.strptime(raw, fmt)
                if fmt in ("%m/%d", "%m-%d"):
                    parsed = parsed.replace(year=current_year)
                candidate = parsed.date()
                if best is None or candidate > best:
                    best = candidate
                break
            except ValueError:
                continue

    return best


_INVALID_PM_VALUES = {"construction", "estimating", "sales", ""}


def is_valid_pm(pm_name: str, pm_config: list) -> bool:
    """True if pm_name is non-empty and matches a known PM's full_name in pm_config.

    Used by OCA's check_missing_pm to flag jobs with no recognised PM, and by
    Casey to decide whether a job has someone to send the customer-update email as.
    """
    name = (pm_name or "").strip().title()
    if not name:
        return False
    if name.upper() == "FERNANDA":
        name = "Fernanda"
    return any(
        pm.get("full_name", "").strip().title() == name
        for pm in pm_config
    )


def _row_to_job(row: dict, sheet_tab: str) -> "dict[str, Any] | None":
    # Normalize all keys to lowercase+stripped so lookups are case-insensitive.
    # This survives any future casing changes the client makes to the header row.
    r = {k.lower().strip(): v for k, v in row.items()}

    def col(name: str) -> str:
        return r.get(name.lower().strip(), "").strip()

    client = col("client")
    if not client:
        return None

    pm_raw = normalize_pm_name(col("project manager"))
    if pm_raw.strip().lower() in _INVALID_PM_VALUES:
        logger.info(f"Row for client '{client}' has no valid PM name (raw='{pm_raw}')")
        pm_name = ""
    else:
        pm_name = pm_raw

    # PRIMARY JOB TYPE is the canonical source; fall back to SECONDARY JOB TYPE
    # if primary is blank; combine as "Primary - Secondary" when both are present.
    primary_type = col("primary job type")
    secondary_type = col("secondary job type")
    if primary_type and secondary_type:
        job_type = f"{primary_type} - {secondary_type}"
    else:
        job_type = primary_type or secondary_type

    return {
        "client_name":              client,
        "pm_name":                  pm_name,
        "job_type":                 job_type,
        "start_date":               col("start date"),
        "estimated_start_window":   col("realistic start date"),
        "deposit_date":             col("deposit date"),
        # Raw sheet column — fallback when no Gmail-based contact history exists.
        "sheet_last_contact":       col("most recent communication"),
        # Retained for casey_active_jobs / dashboard display only.
        "last_pm_contact":          (
            parse_latest_date(col("most recent communication"))
            or parse_latest_date(col("pm communication history"))
        ),
        "pm_communication_history": col("pm communication history"),
        "assigned_crew_sub":        col("subcontractor name"),
        "customer_phone":           col("phone number"),
        "deadline_to_start":        col("dead line to start"),
        "projected_end_date":       col("projected end date"),
        "end_date":                 col("end date"),
        "complaint":                col("complaint/notes"),
        "complaint_note":           col("complaint/notes"),
        "permit":                   col("permit"),
        "total_project":            col("total project"),
        "to_collect":               col("to collect"),
        "client_mood":              col("client mood"),
        "pm_notes":                 col("alfred communication/notes"),
        "email":                    col("email"),
        "job_description":          col("scope of work"),
        "estimator_name":           col("estimator"),
        "address":                  col("address"),
        "new_sub":                  col("new sub?"),
        "sheet_tab":                sheet_tab,
    }


def get_active_jobs() -> list[dict[str, Any]]:
    spreadsheet_id = cfg("google_sheets_id", "GOOGLE_SHEETS_ID")
    if not spreadsheet_id:
        logger.warning("GOOGLE_SHEETS_ID not set — skipping active jobs fetch")
        return []

    jobs: list[dict[str, Any]] = []

    for tab_name, sheet_tab in (("To Start", "to_start"), ("In Process", "in_process")):
        try:
            rows = get_all_rows(spreadsheet_id, tab_name, header_row=2)
        except Exception as e:
            logger.warning(f"Failed to load sheet tab '{tab_name}': {e} — continuing with other tab")
            continue
        if not rows:
            logger.warning(f"Sheet tab '{tab_name}' returned no rows")
            continue
        for row in rows:
            job = _row_to_job(row, sheet_tab)
            if job:
                jobs.append(job)

    logger.info(f"Active jobs found: {len(jobs)}")
    return jobs


def get_jobs_by_client(client_name: str) -> list[dict[str, Any]]:
    jobs = get_active_jobs()
    needle = client_name.strip().lower()
    matches = [j for j in jobs if j["client_name"].lower() == needle]
    logger.info(f"Jobs found for '{client_name}': {len(matches)}")
    return matches


if __name__ == "__main__":
    jobs = get_active_jobs()
    print(f"Found {len(jobs)} active jobs")
    print()
    for job in jobs[:3]:
        for key, value in job.items():
            print(f"  {key}: {value}")
        print()

    print("--- get_jobs_by_client('Mark Parkinson') ---")
    matches = get_jobs_by_client("Mark Parkinson")
    if matches:
        for job in matches:
            for key, value in job.items():
                print(f"  {key}: {value}")
            print()
    else:
        print("  No matches found")
