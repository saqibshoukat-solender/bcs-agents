"""
Truncates job-related tables only. Safe tables (agent_runs, app_config,
pm_config, sales_rep_config, dashboard_users, dashboard_sessions) are NOT touched.

Usage:
    docker compose run --rm casey python scripts/clear_job_data.py --confirm
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


def main() -> None:
    if "--confirm" not in sys.argv:
        print("Safety guard: run with --confirm to actually delete data.")
        print("  docker compose run --rm casey python scripts/clear_job_data.py --confirm")
        sys.exit(1)

    from db.state_store import (
        _db_available, _Session,
        CaseyActiveJob, CaseySentAlert, OcaFlag, CustomerEmailHistory,
    )

    if not _db_available:
        print("ERROR: database not available — check POSTGRES_URL.")
        sys.exit(1)

    with _Session() as s:
        jobs     = s.query(CaseyActiveJob).delete()
        alerts   = s.query(CaseySentAlert).delete()
        flags    = s.query(OcaFlag).delete()
        history  = s.query(CustomerEmailHistory).delete()
        s.commit()

    print(f"Deleted {jobs:>6} rows from casey_active_jobs")
    print(f"Deleted {alerts:>6} rows from casey_sent_alerts")
    print(f"Deleted {flags:>6} rows from oca_flags")
    print(f"Deleted {history:>6} rows from customer_email_history")
    print("Done. Config tables (app_config, pm_config, agent_runs, …) were NOT touched.")


if __name__ == "__main__":
    main()
