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


def _row_to_job(row: dict, sheet_tab: str) -> "dict[str, Any] | None":
    client = row.get("Client", "").strip()
    if not client:
        return None
    pm_raw = normalize_pm_name(row.get("Project Manager", ""))
    if pm_raw.strip().lower() in _INVALID_PM_VALUES:
        logger.warning(f"Skipping row with invalid PM name '{pm_raw}' for client '{client}'")
        return None
    return {
        "client_name":           client,
        "pm_name":               pm_raw,
        "job_type":              row.get("Type of Job", "").strip(),
        "start_date":            row.get("Start Date", "").strip(),
        "estimated_start_window": row.get("Realistic Start Date", "").strip(),
        "deposit_date":          row.get("Deposit Date", "").strip(),
        "most_recent_contact":   row.get("Most Recent communication", "").strip(),
        "last_pm_contact":       (
            parse_latest_date(row.get("Most Recent communication", ""))
            or parse_latest_date(row.get("PM Communication history", ""))
        ),
        "pm_communication_history": row.get("PM Communication history", "").strip(),
        "assigned_crew_sub":     row.get("Contractor", "").strip(),
        "customer_phone":        row.get("Phone number", "").strip(),
        "deadline":              row.get("Dead line to start", "").strip(),
        "overdue":               row.get("Overdue ", "").strip(),
        "projected_end_date":    row.get("Projected End Date", "").strip(),
        "end_date":              row.get("End Date", "").strip(),
        "complaint":             row.get("Complaint", "").strip(),
        "complaint_note":        row.get("Complaint", "").strip(),
        "permit":                row.get("Permit ", "").strip(),
        "total_project":         row.get("Total Project", "").strip(),
        "to_collect":            row.get("To collect", "").strip(),
        "client_mood":           row.get("Client mood", "").strip(),
        "pm_notes":              row.get("Alfred Communication/NOTES", "").strip(),
        "email":                 row.get("Email", "").strip(),
        "job_description":       row.get("INFO", "").strip(),
        "estimator_name":        row.get("Estimator", "").strip(),
        "sheet_tab":             sheet_tab,
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
