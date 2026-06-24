import os
from datetime import datetime, date, timezone, timedelta
from dotenv import load_dotenv
from utils.logger import get_logger

load_dotenv()
logger = get_logger("db.state_store")

# --- DB setup with graceful fallback ---

_db_available = False
_engine = None
_Session = None
Base = None

try:
    from sqlalchemy import (
        create_engine, Column, Integer, String, DateTime, Date,
        Boolean, UniqueConstraint, text, Text,
    )
    from sqlalchemy.orm import declarative_base, sessionmaker

    _engine = create_engine(os.getenv("POSTGRES_URL"), pool_pre_ping=True)
    _Session = sessionmaker(bind=_engine)
    Base = declarative_base()
    _db_available = True
except Exception as e:
    logger.critical(f"PostgreSQL connection failed — running in-memory only: {e}")


# --- ORM models ---

if _db_available:
    class OcaFlag(Base):
        __tablename__ = "oca_flags"

        id = Column(Integer, primary_key=True)
        job_id = Column(String, nullable=False, index=True)
        flag_type = Column(String, nullable=False)
        details = Column(String, nullable=True)
        urgency = Column(String, nullable=True)
        first_flagged_at = Column(DateTime(timezone=True), nullable=False)
        last_alerted_at = Column(DateTime(timezone=True), nullable=True)
        resolved_at = Column(DateTime(timezone=True), nullable=True)
        alert_count = Column(Integer, default=0, nullable=False)

    class OcaRun(Base):
        __tablename__ = "oca_runs"

        id = Column(Integer, primary_key=True)
        run_date = Column(Date, nullable=False, index=True)
        created_at = Column(DateTime(timezone=True), nullable=False)

    class CaseySentAlert(Base):
        __tablename__ = "casey_sent_alerts"

        id = Column(Integer, primary_key=True)
        deal_id = Column(String, nullable=False, index=True)
        alert_type = Column(String, nullable=False)
        sent_at = Column(DateTime(timezone=True), nullable=False)
        deal_name = Column(String, nullable=True)

    class CaseyActiveJob(Base):
        __tablename__ = "casey_active_jobs"
        __table_args__ = (UniqueConstraint("client_name", "pm_name", name="uq_client_pm"),)

        id = Column(Integer, primary_key=True)
        client_name = Column(String, nullable=False)
        pm_name = Column(String, nullable=True)
        job_type = Column(String, nullable=True)
        start_date = Column(String, nullable=True)
        deposit_date = Column(String, nullable=True)
        estimated_start_window = Column(String, nullable=True)
        assigned_crew_sub = Column(String, nullable=True)
        last_pm_contact = Column(Date, nullable=True)
        most_recent_contact = Column(String, nullable=True)
        pm_communication_history = Column(Text, nullable=True)
        hubspot_deal_id = Column(String, nullable=True)
        hubspot_contact_id = Column(String, nullable=True)
        hubspot_owner_name = Column(String, nullable=True)
        customer_email = Column(String, nullable=True)
        customer_phone = Column(String, nullable=True)
        last_customer_update_sent = Column(Date, nullable=True)
        next_scheduled_update = Column(Date, nullable=True)
        escalation_flag = Column(Boolean, default=False, nullable=False)
        escalation_reason = Column(String, nullable=True)
        synced_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        # Extended fields synced from Google Sheet
        client_mood = Column(String, nullable=True)
        complaint_note = Column(String, nullable=True)
        job_description = Column(String, nullable=True)
        estimator_name = Column(String, nullable=True)
        to_collect = Column(String, nullable=True)
        total_project = Column(String, nullable=True)
        sheet_tab = Column(String, nullable=True)
        deadline_to_start = Column(String, nullable=True)

    class CustomerEmailHistory(Base):
        __tablename__ = "customer_email_history"
        __table_args__ = (UniqueConstraint("client_name", "pm_name", name="uq_email_history_client_pm"),)

        id                = Column(Integer, primary_key=True, autoincrement=True)
        client_name       = Column(String, nullable=False)
        pm_name           = Column(String, nullable=False)
        customer_email    = Column(String, nullable=False)
        last_sent_at      = Column(Date, nullable=True)
        last_sent_subject = Column(String, nullable=True)
        email_snippets    = Column(Text, nullable=True)
        fetched_at        = Column(DateTime(timezone=True), nullable=True)

    class AppConfig(Base):
        __tablename__ = "app_config"
        key   = Column(String, primary_key=True)
        value = Column(Text, nullable=True)

    class PmConfig(Base):
        __tablename__ = "pm_config"
        id            = Column(Integer, primary_key=True, autoincrement=True)
        name          = Column(String)
        email         = Column(String)
        slack_user_id = Column(String)

    class SalesRepConfig(Base):
        __tablename__ = "sales_rep_config"
        id               = Column(Integer, primary_key=True, autoincrement=True)
        name             = Column(String)
        email            = Column(String)
        hubspot_owner_id = Column(String)

    class AgentRun(Base):
        __tablename__ = "agent_runs"
        id          = Column(Integer, primary_key=True, autoincrement=True)
        agent       = Column(String)
        started_at  = Column(DateTime, default=datetime.utcnow)
        finished_at = Column(DateTime, nullable=True)
        status      = Column(String)          # running / success / error
        log         = Column(Text, nullable=True)
        summary     = Column(String, nullable=True)  # one-line summary, e.g. "10 emails sent, 2 escalations"

    class DashboardUser(Base):
        __tablename__ = "dashboard_users"
        id            = Column(Integer, primary_key=True, autoincrement=True)
        email         = Column(String, unique=True, nullable=False, index=True)
        password_hash = Column(String, nullable=False)
        created_at    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        updated_at    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class DashboardSession(Base):
        __tablename__ = "dashboard_sessions"
        token      = Column(String, primary_key=True)
        user_id    = Column(Integer, nullable=False, index=True)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        expires_at = Column(DateTime(timezone=True), nullable=False)


# --- In-memory fallback stores ---
_memory_store: dict[tuple[str, str], list[datetime]] = {}
_memory_jobs: dict[tuple[str, str], dict] = {}  # keyed by (client_name, pm_name)


def _row_to_dict(row: "CaseyActiveJob") -> dict:
    return {
        "id": row.id,
        "client_name": row.client_name,
        "pm_name": row.pm_name,
        "job_type": row.job_type,
        "start_date": row.start_date,
        "deposit_date": row.deposit_date,
        "estimated_start_window": row.estimated_start_window,
        "assigned_crew_sub": row.assigned_crew_sub,
        "last_pm_contact": row.last_pm_contact,
        "most_recent_contact": row.most_recent_contact,
        "pm_communication_history": row.pm_communication_history,
        "hubspot_deal_id": row.hubspot_deal_id,
        "hubspot_contact_id": row.hubspot_contact_id,
        "hubspot_owner_name": row.hubspot_owner_name,
        "customer_email": row.customer_email,
        "customer_phone": row.customer_phone,
        "last_customer_update_sent": row.last_customer_update_sent,
        "next_scheduled_update": row.next_scheduled_update,
        "escalation_flag": row.escalation_flag,
        "escalation_reason": row.escalation_reason,
        "synced_at": row.synced_at,
        "client_mood": row.client_mood,
        "complaint_note": row.complaint_note,
        "job_description": row.job_description,
        "estimator_name": row.estimator_name,
        "to_collect": row.to_collect,
        "total_project": row.total_project,
        "sheet_tab": row.sheet_tab,
        "deadline_to_start": row.deadline_to_start,
    }


# --- Table setup ---

def init_db() -> None:
    if not _db_available:
        logger.warning("init_db: DB not available, skipping")
        return
    try:
        Base.metadata.create_all(_engine)
        with _engine.connect() as conn:
            conn.execute(text("ALTER TABLE oca_flags ADD COLUMN IF NOT EXISTS details TEXT"))
            conn.execute(text("ALTER TABLE oca_flags ADD COLUMN IF NOT EXISTS urgency TEXT"))
            # Extended job fields
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS hubspot_contact_id TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS client_mood TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS complaint_note TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS job_description TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS estimator_name TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS to_collect TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS total_project TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS sheet_tab VARCHAR"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS pm_communication_history TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS deadline_to_start VARCHAR"))
            conn.execute(text("ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS summary VARCHAR"))
            conn.commit()
            # Seed default HubSpot field names if not already set
            _hs_defaults = {
                "hubspot_field_pm_name":           "pm_name",
                "hubspot_field_crew_confirmed":    "crew_confirmed",
                "hubspot_field_last_update_sent":  "last_customer_update_sent",
                "hubspot_field_next_update":       "next_scheduled_update",
                "hubspot_field_escalation_flag":   "escalation_flag",
                "hubspot_field_escalation_reason": "escalation_reason",
                "hubspot_portal_id":               "51566851",
            }
            for key, default_val in _hs_defaults.items():
                existing = conn.execute(
                    text("SELECT value FROM app_config WHERE key = :k"), {"k": key}
                ).fetchone()
                if not existing:
                    conn.execute(
                        text("INSERT INTO app_config (key, value) VALUES (:k, :v)"),
                        {"k": key, "v": default_val},
                    )
            conn.commit()

            # One-time migration: the Gmail history cache predates the
            # [BCS Update] subject filter, so it may contain Casey's own
            # automated emails mixed into "PM contact" history. Force every
            # row to be re-fetched on the next OCA run, then never run again.
            already_cleared = conn.execute(
                text("SELECT value FROM app_config WHERE key = 'gmail_cache_cleared'")
            ).fetchone()
            if not already_cleared:
                conn.execute(text("UPDATE customer_email_history SET fetched_at = NULL"))
                conn.execute(
                    text("INSERT INTO app_config (key, value) VALUES ('gmail_cache_cleared', 'true')")
                )
                conn.commit()
                logger.info("One-time migration: cleared customer_email_history.fetched_at to force Gmail re-fetch")
        logger.info("Database tables verified/created")
    except Exception as e:
        logger.error(f"init_db error: {e}")


try:
    if _db_available:
        init_db()
except Exception as e:
    logger.error(f"Failed to initialise tables on import: {e}")


# --- AppConfig helpers ---

def get_config(key: str) -> "str | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            row = s.query(AppConfig).filter_by(key=key).first()
            return row.value if row else None
    except Exception as e:
        logger.error(f"get_config({key}): {e}")
        return None


def set_config(key: str, value: str) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            row = s.query(AppConfig).filter_by(key=key).first()
            if row:
                row.value = value
            else:
                s.add(AppConfig(key=key, value=value))
            s.commit()
    except Exception as e:
        logger.error(f"set_config({key}): {e}")


def get_all_config() -> dict:
    if not _db_available:
        return {}
    try:
        with _Session() as s:
            return {r.key: r.value for r in s.query(AppConfig).all()}
    except Exception as e:
        logger.error(f"get_all_config: {e}")
        return {}


# --- PM config helpers ---

def get_pm_list() -> list:
    if not _db_available:
        return []
    try:
        with _Session() as s:
            return [
                {"id": r.id, "full_name": r.name, "email": r.email, "slack_user_id": r.slack_user_id}
                for r in s.query(PmConfig).order_by(PmConfig.id).all()
            ]
    except Exception as e:
        logger.error(f"get_pm_list: {e}")
        return []


def add_pm(name: str, email: str, slack_user_id: str) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            s.add(PmConfig(name=name, email=email, slack_user_id=slack_user_id))
            s.commit()
    except Exception as e:
        logger.error(f"add_pm: {e}")


def delete_pm(pm_id: int) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            row = s.query(PmConfig).filter_by(id=pm_id).first()
            if row:
                s.delete(row)
                s.commit()
    except Exception as e:
        logger.error(f"delete_pm({pm_id}): {e}")


def seed_pm_config() -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            if s.query(PmConfig).count() > 0:
                return
        import json, pathlib
        pm_json = pathlib.Path(__file__).parent.parent / "config" / "pm_config.json"
        if not pm_json.exists():
            return
        pms = json.loads(pm_json.read_text())
        with _Session() as s:
            for pm in pms:
                s.add(PmConfig(
                    name=pm.get("full_name", ""),
                    email=pm.get("email", ""),
                    slack_user_id=pm.get("slack_user_id", ""),
                ))
            s.commit()
        logger.info(f"Seeded {len(pms)} PMs into pm_config table")
    except Exception as e:
        logger.error(f"seed_pm_config: {e}")


# --- Sales rep helpers ---

def get_sales_rep_list() -> list:
    if not _db_available:
        return []
    try:
        with _Session() as s:
            return [
                {"id": r.id, "name": r.name, "email": r.email, "hubspot_owner_id": r.hubspot_owner_id}
                for r in s.query(SalesRepConfig).order_by(SalesRepConfig.id).all()
            ]
    except Exception as e:
        logger.error(f"get_sales_rep_list: {e}")
        return []


def add_sales_rep(name: str, email: str, hubspot_owner_id: str) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            s.add(SalesRepConfig(name=name, email=email, hubspot_owner_id=hubspot_owner_id))
            s.commit()
    except Exception as e:
        logger.error(f"add_sales_rep: {e}")


def delete_sales_rep(rep_id: int) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            row = s.query(SalesRepConfig).filter_by(id=rep_id).first()
            if row:
                s.delete(row)
                s.commit()
    except Exception as e:
        logger.error(f"delete_sales_rep({rep_id}): {e}")


# --- AgentRun helpers ---

def create_agent_run(agent: str) -> int:
    if not _db_available:
        return -1
    try:
        with _Session() as s:
            run = AgentRun(agent=agent, started_at=datetime.utcnow(), status="running", log="")
            s.add(run)
            s.commit()
            s.refresh(run)
            return run.id
    except Exception as e:
        logger.error(f"create_agent_run: {e}")
        return -1


def append_agent_run_log(run_id: int, lines: str) -> None:
    if not _db_available or run_id < 0:
        return
    try:
        with _Session() as s:
            s.execute(
                text("UPDATE agent_runs SET log = COALESCE(log,'') || :lines WHERE id = :id"),
                {"lines": lines, "id": run_id},
            )
            s.commit()
    except Exception as e:
        logger.error(f"append_agent_run_log: {e}")


def finish_agent_run(run_id: int, status: str, summary: str = "") -> None:
    if not _db_available or run_id < 0:
        return
    try:
        with _Session() as s:
            run = s.query(AgentRun).filter_by(id=run_id).first()
            if run:
                run.status = status
                run.finished_at = datetime.utcnow()
                if summary:
                    run.summary = summary
                s.commit()
    except Exception as e:
        logger.error(f"finish_agent_run: {e}")


def set_agent_run_summary(run_id: int, summary: str) -> None:
    """Set the one-line summary on a run without touching status/finished_at.

    Used when a run's lifecycle (status/finished_at) is owned by something
    else — e.g. the dashboard's subprocess wrapper — but the run itself still
    wants to record what it did.
    """
    if not _db_available or run_id < 0:
        return
    try:
        with _Session() as s:
            run = s.query(AgentRun).filter_by(id=run_id).first()
            if run:
                run.summary = summary
                s.commit()
    except Exception as e:
        logger.error(f"set_agent_run_summary: {e}")


def get_agent_run(run_id: int) -> "dict | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            r = s.query(AgentRun).filter_by(id=run_id).first()
            if not r:
                return None
            return {
                "id": r.id, "agent": r.agent, "status": r.status,
                "log": r.log or "", "started_at": r.started_at, "finished_at": r.finished_at,
                "summary": r.summary or "",
            }
    except Exception as e:
        logger.error(f"get_agent_run: {e}")
        return None


def get_last_agent_run(agent: str) -> "dict | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            r = s.query(AgentRun).filter_by(agent=agent).order_by(AgentRun.id.desc()).first()
            if not r:
                return None
            return {
                "id": r.id, "agent": r.agent, "status": r.status,
                "log": r.log or "", "started_at": r.started_at, "finished_at": r.finished_at,
                "summary": r.summary or "",
            }
    except Exception as e:
        logger.error(f"get_last_agent_run: {e}")
        return None


def get_run_logs(agent: "str | None" = None, limit: int = 50) -> list[dict]:
    """Return the most recent agent runs (any/both agents), newest first."""
    if not _db_available:
        return []
    try:
        with _Session() as s:
            q = s.query(AgentRun)
            if agent:
                q = q.filter_by(agent=agent)
            rows = q.order_by(AgentRun.id.desc()).limit(limit).all()
            return [
                {
                    "id": r.id, "agent": r.agent, "status": r.status,
                    "log": r.log or "", "started_at": r.started_at, "finished_at": r.finished_at,
                    "summary": r.summary or "",
                }
                for r in rows
            ]
    except Exception as e:
        logger.error(f"get_run_logs: {e}")
        return []


# --- Dashboard auth (users + sessions) ---

def get_user_by_email(email: str) -> "dict | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            r = s.query(DashboardUser).filter_by(email=email.strip().lower()).first()
            if not r:
                return None
            return {"id": r.id, "email": r.email, "password_hash": r.password_hash}
    except Exception as e:
        logger.error(f"get_user_by_email: {e}")
        return None


def get_user_by_id(user_id: int) -> "dict | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            r = s.query(DashboardUser).filter_by(id=user_id).first()
            if not r:
                return None
            return {"id": r.id, "email": r.email, "password_hash": r.password_hash}
    except Exception as e:
        logger.error(f"get_user_by_id: {e}")
        return None


def create_user(email: str, password_hash: str) -> bool:
    if not _db_available:
        return False
    try:
        with _Session() as s:
            email = email.strip().lower()
            if s.query(DashboardUser).filter_by(email=email).first():
                return False
            s.add(DashboardUser(email=email, password_hash=password_hash))
            s.commit()
            return True
    except Exception as e:
        logger.error(f"create_user: {e}")
        return False


def update_user_password(email: str, password_hash: str) -> bool:
    if not _db_available:
        return False
    try:
        with _Session() as s:
            r = s.query(DashboardUser).filter_by(email=email.strip().lower()).first()
            if not r:
                return False
            r.password_hash = password_hash
            r.updated_at = datetime.now(timezone.utc)
            s.commit()
            return True
    except Exception as e:
        logger.error(f"update_user_password: {e}")
        return False


def create_session(user_id: int, token: str, expires_at: datetime) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            s.add(DashboardSession(token=token, user_id=user_id, expires_at=expires_at))
            s.commit()
    except Exception as e:
        logger.error(f"create_session: {e}")


def get_session(token: str) -> "dict | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            r = s.query(DashboardSession).filter_by(token=token).first()
            if not r:
                return None
            return {"token": r.token, "user_id": r.user_id, "expires_at": r.expires_at}
    except Exception as e:
        logger.error(f"get_session: {e}")
        return None


def delete_session(token: str) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            r = s.query(DashboardSession).filter_by(token=token).first()
            if r:
                s.delete(r)
                s.commit()
    except Exception as e:
        logger.error(f"delete_session: {e}")


# --- Casey active jobs ---

def upsert_active_job(job: dict) -> None:
    client_name = job["client_name"]
    pm_name = job.get("pm_name") or ""
    now = datetime.now(timezone.utc)

    if not _db_available:
        key = (client_name, pm_name)
        existing = _memory_jobs.get(key)
        if existing:
            for field in ("job_type", "start_date", "deposit_date", "estimated_start_window",
                          "assigned_crew_sub", "last_pm_contact", "most_recent_contact",
                          "pm_communication_history",
                          "hubspot_deal_id", "hubspot_contact_id", "hubspot_owner_name",
                          "customer_email", "customer_phone", "client_mood", "complaint_note",
                          "job_description", "estimator_name", "to_collect", "total_project",
                          "sheet_tab", "deadline_to_start"):
                existing[field] = job.get(field)
            existing["synced_at"] = now
        else:
            _memory_jobs[key] = {**job, "last_customer_update_sent": None,
                                  "next_scheduled_update": None, "escalation_flag": False,
                                  "escalation_reason": None, "synced_at": now}
        return

    try:
        with _Session() as session:
            row = (
                session.query(CaseyActiveJob)
                .filter_by(client_name=client_name, pm_name=pm_name)
                .first()
            )
            if row:
                row.job_type = job.get("job_type")
                row.start_date = job.get("start_date")
                row.deposit_date = job.get("deposit_date")
                row.estimated_start_window = job.get("estimated_start_window")
                row.assigned_crew_sub = job.get("assigned_crew_sub")
                row.last_pm_contact = job.get("last_pm_contact")
                row.most_recent_contact = job.get("most_recent_contact")
                row.pm_communication_history = job.get("pm_communication_history")
                row.hubspot_deal_id = job.get("hubspot_deal_id")
                row.hubspot_owner_name = job.get("hubspot_owner_name")
                row.customer_email = job.get("customer_email")
                row.customer_phone = job.get("customer_phone")
                row.client_mood = job.get("client_mood")
                row.complaint_note = job.get("complaint_note")
                row.job_description = job.get("job_description")
                row.estimator_name = job.get("estimator_name")
                row.to_collect = job.get("to_collect")
                row.total_project = job.get("total_project")
                row.sheet_tab = job.get("sheet_tab")
                row.deadline_to_start = job.get("deadline_to_start")
                row.synced_at = now
                # Only overwrite hubspot_contact_id if provided
                if job.get("hubspot_contact_id"):
                    row.hubspot_contact_id = job["hubspot_contact_id"]
                # Never overwrite scheduling fields if already set
                if not row.last_customer_update_sent and job.get("last_customer_update_sent"):
                    row.last_customer_update_sent = job["last_customer_update_sent"]
                if not row.next_scheduled_update and job.get("next_scheduled_update"):
                    row.next_scheduled_update = job["next_scheduled_update"]
            else:
                session.add(CaseyActiveJob(
                    client_name=client_name,
                    pm_name=pm_name,
                    job_type=job.get("job_type"),
                    start_date=job.get("start_date"),
                    deposit_date=job.get("deposit_date"),
                    estimated_start_window=job.get("estimated_start_window"),
                    assigned_crew_sub=job.get("assigned_crew_sub"),
                    last_pm_contact=job.get("last_pm_contact"),
                    most_recent_contact=job.get("most_recent_contact"),
                    pm_communication_history=job.get("pm_communication_history"),
                    hubspot_deal_id=job.get("hubspot_deal_id"),
                    hubspot_contact_id=job.get("hubspot_contact_id"),
                    hubspot_owner_name=job.get("hubspot_owner_name"),
                    customer_email=job.get("customer_email"),
                    customer_phone=job.get("customer_phone"),
                    client_mood=job.get("client_mood"),
                    complaint_note=job.get("complaint_note"),
                    job_description=job.get("job_description"),
                    estimator_name=job.get("estimator_name"),
                    to_collect=job.get("to_collect"),
                    total_project=job.get("total_project"),
                    sheet_tab=job.get("sheet_tab"),
                    deadline_to_start=job.get("deadline_to_start"),
                    escalation_flag=False,
                    synced_at=now,
                ))
            session.commit()
    except Exception as e:
        logger.error(f"DB error in upsert_active_job ({client_name}): {e}")


def get_jobs_due_for_update() -> list[dict]:
    today = date.today()

    if not _db_available:
        return [
            v for v in _memory_jobs.values()
            if not v.get("escalation_flag")
            and (v.get("next_scheduled_update") is None or v["next_scheduled_update"] <= today)
        ]

    try:
        with _Session() as session:
            rows = (
                session.query(CaseyActiveJob)
                .filter(
                    CaseyActiveJob.escalation_flag == False,
                    (CaseyActiveJob.next_scheduled_update == None) |
                    (CaseyActiveJob.next_scheduled_update <= today),
                )
                .all()
            )
            return [_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.error(f"DB error in get_jobs_due_for_update: {e}")
        return []


def get_escalated_jobs() -> list[dict]:
    if not _db_available:
        return [v for v in _memory_jobs.values() if v.get("escalation_flag")]

    try:
        with _Session() as session:
            rows = session.query(CaseyActiveJob).filter_by(escalation_flag=True).all()
            return [_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.error(f"DB error in get_escalated_jobs: {e}")
        return []


def get_all_active_jobs() -> list[dict]:
    if not _db_available:
        return list(_memory_jobs.values())
    try:
        with _Session() as session:
            return [_row_to_dict(r) for r in session.query(CaseyActiveJob).all()]
    except Exception as e:
        logger.error(f"DB error in get_all_active_jobs: {e}")
        return []


def set_hubspot_deal_id(client_name: str, pm_name: str, deal_id: str) -> None:
    if not _db_available:
        key = (client_name, pm_name or "")
        if key in _memory_jobs:
            _memory_jobs[key]["hubspot_deal_id"] = deal_id
        return
    try:
        with _Session() as session:
            row = session.query(CaseyActiveJob).filter_by(
                client_name=client_name, pm_name=pm_name or ""
            ).first()
            if row:
                row.hubspot_deal_id = deal_id
                session.commit()
    except Exception as e:
        logger.error(f"DB error in set_hubspot_deal_id ({client_name}): {e}")


def set_hubspot_contact_id(client_name: str, pm_name: str, contact_id: str) -> None:
    if not _db_available:
        key = (client_name, pm_name or "")
        if key in _memory_jobs:
            _memory_jobs[key]["hubspot_contact_id"] = contact_id
        return
    try:
        with _Session() as session:
            row = session.query(CaseyActiveJob).filter_by(
                client_name=client_name, pm_name=pm_name or ""
            ).first()
            if row:
                row.hubspot_contact_id = contact_id
                session.commit()
    except Exception as e:
        logger.error(f"DB error in set_hubspot_contact_id ({client_name}): {e}")


def set_update_sent(client_name: str, pm_name: str) -> None:
    today = date.today()
    next_update = today + timedelta(days=7)
    now = datetime.now(timezone.utc)

    if not _db_available:
        key = (client_name, pm_name or "")
        if key in _memory_jobs:
            _memory_jobs[key]["last_customer_update_sent"] = today
            _memory_jobs[key]["next_scheduled_update"] = next_update
        return

    try:
        with _Session() as session:
            row = session.query(CaseyActiveJob).filter_by(
                client_name=client_name, pm_name=pm_name or ""
            ).first()
            if row:
                row.last_customer_update_sent = today
                row.next_scheduled_update = next_update
                row.synced_at = now
                session.commit()
    except Exception as e:
        logger.error(f"DB error in set_update_sent ({client_name}): {e}")


def set_next_scheduled_update(client_name: str, pm_name: str, next_date: date) -> None:
    """Set next_scheduled_update without marking last_customer_update_sent.

    Used when the welcome-email window has passed for a job that has never
    been emailed — defers the job into the normal update cadence.
    """
    if not _db_available:
        key = (client_name, pm_name or "")
        if key in _memory_jobs:
            _memory_jobs[key]["next_scheduled_update"] = next_date
        return

    try:
        with _Session() as session:
            row = session.query(CaseyActiveJob).filter_by(
                client_name=client_name, pm_name=pm_name or ""
            ).first()
            if row:
                row.next_scheduled_update = next_date
                session.commit()
    except Exception as e:
        logger.error(f"DB error in set_next_scheduled_update ({client_name}): {e}")


def set_escalation(client_name: str, pm_name: str, reason: str) -> None:
    if not _db_available:
        key = (client_name, pm_name or "")
        if key in _memory_jobs:
            _memory_jobs[key]["escalation_flag"] = True
            _memory_jobs[key]["escalation_reason"] = reason
        return

    try:
        with _Session() as session:
            row = session.query(CaseyActiveJob).filter_by(
                client_name=client_name, pm_name=pm_name or ""
            ).first()
            if row:
                row.escalation_flag = True
                row.escalation_reason = reason
                session.commit()
    except Exception as e:
        logger.error(f"DB error in set_escalation ({client_name}): {e}")


def clear_escalation(client_name: str, pm_name: str) -> None:
    if not _db_available:
        key = (client_name, pm_name or "")
        if key in _memory_jobs:
            _memory_jobs[key]["escalation_flag"] = False
            _memory_jobs[key]["escalation_reason"] = None
        return

    try:
        with _Session() as session:
            row = session.query(CaseyActiveJob).filter_by(
                client_name=client_name, pm_name=pm_name or ""
            ).first()
            if row:
                row.escalation_flag = False
                row.escalation_reason = None
                session.commit()
    except Exception as e:
        logger.error(f"DB error in clear_escalation ({client_name}): {e}")


def get_summary() -> dict[str, int]:
    today = date.today()

    if not _db_available:
        all_jobs = list(_memory_jobs.values())
        escalated = sum(1 for j in all_jobs if j.get("escalation_flag"))
        due = sum(
            1 for j in all_jobs
            if not j.get("escalation_flag")
            and (j.get("next_scheduled_update") is None or j["next_scheduled_update"] <= today)
        )
        up_to_date = len(all_jobs) - escalated - due
        return {"total": len(all_jobs), "due_for_update": due, "escalated": escalated, "up_to_date": max(up_to_date, 0)}

    try:
        with _Session() as session:
            all_rows = session.query(CaseyActiveJob).all()
            total = len(all_rows)
            escalated = sum(1 for r in all_rows if r.escalation_flag)
            due = sum(
                1 for r in all_rows
                if not r.escalation_flag
                and (r.next_scheduled_update is None or r.next_scheduled_update <= today)
            )
            return {"total": total, "due_for_update": due, "escalated": escalated, "up_to_date": total - escalated - due}
    except Exception as e:
        logger.error(f"DB error in get_summary: {e}")
        return {"total": 0, "due_for_update": 0, "escalated": 0, "up_to_date": 0}


# --- Customer email history (Gmail-based contact tracking) ---

def get_email_history(client_name: str, pm_name: str) -> "CustomerEmailHistory | None":
    if not _db_available:
        return None
    try:
        with _Session() as session:
            row = (
                session.query(CustomerEmailHistory)
                .filter_by(client_name=client_name, pm_name=pm_name or "")
                .first()
            )
            if row:
                session.expunge(row)
            return row
    except Exception as e:
        logger.error(f"DB error in get_email_history ({client_name}): {e}")
        return None


def upsert_email_history(
    client_name: str,
    pm_name: str,
    customer_email: str,
    last_sent_at: "date | None",
    last_sent_subject: str,
    email_snippets: str,
    fetched_at: datetime,
) -> None:
    if not _db_available:
        return
    try:
        with _Session() as session:
            row = (
                session.query(CustomerEmailHistory)
                .filter_by(client_name=client_name, pm_name=pm_name or "")
                .first()
            )
            if row:
                row.customer_email = customer_email
                row.last_sent_at = last_sent_at
                row.last_sent_subject = last_sent_subject
                row.email_snippets = email_snippets
                row.fetched_at = fetched_at
            else:
                session.add(CustomerEmailHistory(
                    client_name=client_name,
                    pm_name=pm_name or "",
                    customer_email=customer_email,
                    last_sent_at=last_sent_at,
                    last_sent_subject=last_sent_subject,
                    email_snippets=email_snippets,
                    fetched_at=fetched_at,
                ))
            session.commit()
    except Exception as e:
        logger.error(f"DB error in upsert_email_history ({client_name}): {e}")


def should_fetch_email_history(client_name: str, pm_name: str) -> bool:
    """True if no row exists yet, or the row's fetched_at is more than 23 hours old."""
    if not _db_available:
        return True
    try:
        with _Session() as session:
            row = (
                session.query(CustomerEmailHistory)
                .filter_by(client_name=client_name, pm_name=pm_name or "")
                .first()
            )
            if not row or row.fetched_at is None:
                return True
            fetched_at = row.fetched_at
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            return fetched_at < datetime.now(timezone.utc) - timedelta(hours=23)
    except Exception as e:
        logger.error(f"DB error in should_fetch_email_history ({client_name}): {e}")
        return True


# --- Casey alert deduplication (unchanged) ---

def was_alert_sent_today(deal_id: str, alert_type: str) -> bool:
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    if not _db_available:
        times = _memory_store.get((deal_id, alert_type), [])
        return any(t >= today_start for t in times)

    try:
        with _Session() as session:
            return (
                session.query(CaseySentAlert)
                .filter(
                    CaseySentAlert.deal_id == deal_id,
                    CaseySentAlert.alert_type == alert_type,
                    CaseySentAlert.sent_at >= today_start,
                )
                .first()
            ) is not None
    except Exception as e:
        logger.error(f"DB error in was_alert_sent_today: {e}")
        return False


def get_alert_sent_at_today(deal_id: str, alert_type: str) -> "datetime | None":
    """Debug helper: returns the casey_sent_alerts.sent_at timestamp behind
    was_alert_sent_today(), or None if no matching row exists for today."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    if not _db_available:
        times = [t for t in _memory_store.get((deal_id, alert_type), []) if t >= today_start]
        return max(times) if times else None

    try:
        with _Session() as session:
            row = (
                session.query(CaseySentAlert)
                .filter(
                    CaseySentAlert.deal_id == deal_id,
                    CaseySentAlert.alert_type == alert_type,
                    CaseySentAlert.sent_at >= today_start,
                )
                .first()
            )
            return row.sent_at if row else None
    except Exception as e:
        logger.error(f"DB error in get_alert_sent_at_today: {e}")
        return None


def was_escalation_sent_recently(deal_id: str, days: int = 3) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    if not _db_available:
        times = _memory_store.get((deal_id, "escalation"), [])
        return any(t >= cutoff for t in times)

    try:
        with _Session() as session:
            return (
                session.query(CaseySentAlert)
                .filter(
                    CaseySentAlert.deal_id == deal_id,
                    CaseySentAlert.alert_type == "escalation",
                    CaseySentAlert.sent_at >= cutoff,
                )
                .first()
            ) is not None
    except Exception as e:
        logger.error(f"DB error in was_escalation_sent_recently: {e}")
        return False


def record_alert_sent(deal_id: str, alert_type: str, deal_name: str) -> None:
    now = datetime.now(timezone.utc)

    if not _db_available:
        _memory_store.setdefault((deal_id, alert_type), []).append(now)
        return

    try:
        with _Session() as session:
            session.add(CaseySentAlert(
                deal_id=deal_id,
                alert_type=alert_type,
                sent_at=now,
                deal_name=deal_name,
            ))
            session.commit()
    except Exception as e:
        logger.error(f"DB error in record_alert_sent: {e}")


# --- OCA flag functions ---

def flag_exists(job_id: str, flag_type: str) -> bool:
    if not _db_available:
        return False
    try:
        with _Session() as session:
            return (
                session.query(OcaFlag)
                .filter_by(job_id=job_id, flag_type=flag_type, resolved_at=None)
                .first()
            ) is not None
    except Exception as e:
        logger.error(f"DB error in flag_exists: {e}")
        return False


def create_flag(job_id: str, flag_type: str, details: str = "", urgency: str = "") -> None:
    if not _db_available:
        return
    now = datetime.now(timezone.utc)
    try:
        with _Session() as session:
            session.add(OcaFlag(
                job_id=job_id,
                flag_type=flag_type,
                details=details or None,
                urgency=urgency or None,
                first_flagged_at=now,
                last_alerted_at=now,
                alert_count=1,
            ))
            session.commit()
    except Exception as e:
        logger.error(f"DB error in create_flag ({job_id}/{flag_type}): {e}")


def should_alert_again(job_id: str, flag_type: str, cooldown_hours: int = 24) -> bool:
    if not _db_available:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    try:
        with _Session() as session:
            flag = (
                session.query(OcaFlag)
                .filter_by(job_id=job_id, flag_type=flag_type, resolved_at=None)
                .first()
            )
            if not flag:
                return True
            if flag.last_alerted_at is None:
                return True
            return flag.last_alerted_at < cutoff
    except Exception as e:
        logger.error(f"DB error in should_alert_again: {e}")
        return False


def get_flag_alert_age_hours(job_id: str, flag_type: str) -> "float | None":
    """Hours since the active (unresolved) flag was last alerted, or None if no such flag/timestamp."""
    if not _db_available:
        return None
    try:
        with _Session() as session:
            flag = (
                session.query(OcaFlag)
                .filter_by(job_id=job_id, flag_type=flag_type, resolved_at=None)
                .first()
            )
            if not flag or flag.last_alerted_at is None:
                return None
            return (datetime.now(timezone.utc) - flag.last_alerted_at).total_seconds() / 3600
    except Exception as e:
        logger.error(f"DB error in get_flag_alert_age_hours: {e}")
        return None


def update_flag_alerted(job_id: str, flag_type: str) -> bool:
    if not _db_available:
        return False
    try:
        with _Session() as session:
            flag = (
                session.query(OcaFlag)
                .filter_by(job_id=job_id, flag_type=flag_type, resolved_at=None)
                .first()
            )
            if not flag:
                return False
            flag.last_alerted_at = datetime.now(timezone.utc)
            flag.alert_count = (flag.alert_count or 0) + 1
            session.commit()
            return True
    except Exception as e:
        logger.error(f"DB error in update_flag_alerted: {e}")
        return False


# Keep old name as alias for backward compatibility with alerts.py stub
def update_flag(job_id: str, flag_type: str) -> bool:
    return update_flag_alerted(job_id, flag_type)


def resolve_flag(job_id: str, flag_type: str) -> bool:
    if not _db_available:
        return False
    try:
        with _Session() as session:
            flag = (
                session.query(OcaFlag)
                .filter_by(job_id=job_id, flag_type=flag_type, resolved_at=None)
                .first()
            )
            if not flag:
                return False
            flag.resolved_at = datetime.now(timezone.utc)
            session.commit()
            return True
    except Exception as e:
        logger.error(f"DB error in resolve_flag: {e}")
        return False


def resolve_flags_not_in(active_job_ids: list, flag_type: str) -> None:
    if not _db_available:
        return
    now = datetime.now(timezone.utc)
    try:
        with _Session() as session:
            flags = (
                session.query(OcaFlag)
                .filter(
                    OcaFlag.flag_type == flag_type,
                    OcaFlag.resolved_at == None,
                    OcaFlag.job_id.notin_(active_job_ids) if active_job_ids else True,
                )
                .all()
            )
            for flag in flags:
                flag.resolved_at = now
                logger.info(f"Auto-resolved {flag_type} flag for {flag.job_id}")
            session.commit()
    except Exception as e:
        logger.error(f"DB error in resolve_flags_not_in ({flag_type}): {e}")


def get_active_flags_summary() -> dict:
    summary = {
        "stale": 0, "missing_pm": 0, "unconfirmed_crew": 0,
        "dropped_invoice": 0, "readiness_sync": 0,
    }
    if not _db_available:
        return summary
    try:
        with _Session() as session:
            rows = session.query(OcaFlag).filter(OcaFlag.resolved_at == None).all()
            for row in rows:
                ft = row.flag_type
                if ft == "stale_record":
                    summary["stale"] += 1
                elif ft in summary:
                    summary[ft] += 1
    except Exception as e:
        logger.error(f"DB error in get_active_flags_summary: {e}")
    return summary


def get_weekly_summary() -> dict:
    today = date.today()
    # Monday of this week
    week_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) - timedelta(days=today.weekday())
    result = {"raised": 0, "resolved": 0, "active": 0}
    if not _db_available:
        return result
    try:
        with _Session() as session:
            all_flags = session.query(OcaFlag).all()
            for flag in all_flags:
                if flag.first_flagged_at and flag.first_flagged_at >= week_start:
                    result["raised"] += 1
                if flag.resolved_at and flag.resolved_at >= week_start:
                    result["resolved"] += 1
                if flag.resolved_at is None:
                    result["active"] += 1
    except Exception as e:
        logger.error(f"DB error in get_weekly_summary: {e}")
    return result


def is_first_run_today() -> bool:
    today = date.today()
    if not _db_available:
        return True
    try:
        with _Session() as session:
            existing = session.query(OcaRun).filter_by(run_date=today).first()
            if existing:
                return False
            session.add(OcaRun(run_date=today, created_at=datetime.now(timezone.utc)))
            session.commit()
            return True
    except Exception as e:
        logger.error(f"DB error in is_first_run_today: {e}")
        return False
