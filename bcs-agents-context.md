# BCS Agents — Project Context for Claude Code

## Project Overview
Building two AI agents for Blue Collar Scholars (BCS), a home improvement company in DC area.

- **Casey** — Customer Success Agent: monitors active jobs, sends customer update emails every 7 days, escalates stale jobs to Josh via Slack
- **OCA** — Operations Control Agent: internal watchdog, runs hourly, detects operational issues and alerts the team

## Stack
- Python 3.11, Docker, PostgreSQL
- HubSpot API, Google Sheets API, Slack SDK, Gmail API (domain-wide delegation), Anthropic API
- Hosted on Railway (planned)

## Project Location
```
~/trisage solutions codes/bcs-agents/
```

## Key Discovery: Data Structure
BCS does NOT use HubSpot Tickets or the Current Projects pipeline for active jobs.
**Source of truth for active jobs = Google Sheet "In Process" tab**

- Sheet ID: `19hgRkkhn7B9F-LdUIy0pW_zXL4HXnEXeC7rSY8JDLTc`
- Tab: "In Process" — 61 active jobs
- Row 1: owner names (ignore), Row 2: real headers, Row 3+: data
- Key columns: Client, Project Manager, Type of Job, Deposit Date, PM Communication history (multiple dates per cell newline-separated), Most Recent communication, Realistic Start Date, Start Date, Contractor, Email, Phone number, Total Project, To collect

## Credentials (in .env)
```
HUBSPOT_ACCESS_TOKEN=pat-na1-2da33b46-aaf0-400f-be38-259598134e38
SLACK_BOT_TOKEN=xoxb-...
GOOGLE_SHEETS_ID=19hgRkkhn7B9F-LdUIy0pW_zXL4HXnEXeC7rSY8JDLTc
GOOGLE_SERVICE_ACCOUNT_JSON=/app/service_account.json
GOOGLE_WORKSPACE_DOMAIN=bluecollarscholars.net
ANTHROPIC_API_KEY=[set]
POSTGRES_URL=postgresql://bcs_user:...@postgres:5432/bcs_agents
JOSH_SLACK_USER_ID=[set to developer's own ID for testing]
SAM_SLACK_USER_ID=[needed]
SLACK_DAILY_CHANNEL=casey-daily
SLACK_OCA_CHANNEL=oca-alerts
```

## PM Config (`config/pm_config.json`)
Real PM data — all 10 PMs with full name, email, Slack user ID:
- Tatiana Moreno, Gustavo Zuluaga, Laura Peña, Julie Martinez
- Dan Diazgranados, Daniel Gomez Cortez, Laura Arbelaez, Andrea Ortega
- Esteban Estarita, Alfredo Núñez
All on `@bluecollarscholars.net` domain

## PM Name Normalization (in sheets.py)
Sheet uses abbreviations: Lau→Laura Peña, Santi→Santiago, Julie→Julie Martinez, Tavo→Gustavo Zuluaga, Alfred→Alfredo Núñez, Steve→Esteban Estarita

## HubSpot Structure
- 3811 total deals, all in `default` pipeline (BCS Prospects Pipeline)
- Stages: Estimate booked → Estimate Completed → Estimate Sent → Deposit Invoice Sent → Closed Won/Lost
- `Deposit Invoice Sent` (stage id: `1315907842`) — 20 deals, most relevant for active jobs
- `Closed Won` — 652 deals (mix of active and finished, no way to distinguish in HubSpot)
- Current Projects pipeline (`790250649`) — exists but EMPTY, never used
- Deal owners = sales reps (Jordan Hantman, Christian Audé, Lucia Fuente Buena, Karen Garrido, etc.) NOT project managers
- 48 owners loaded and cached

## Database Tables
- `casey_active_jobs` — main job tracking table
  - client_name, pm_name, job_type, start_date, deposit_date, estimated_start_window, assigned_crew_sub, last_pm_contact (DATE), most_recent_contact, hubspot_deal_id, hubspot_owner_name, customer_email, customer_phone, last_customer_update_sent, next_scheduled_update, escalation_flag, escalation_reason, synced_at
  - UNIQUE(client_name, pm_name)
- `casey_sent_alerts` — deduplication table
  - deal_id, alert_type, sent_at, deal_name
- `oca_flags` — OCA issue tracking
  - job_id, flag_type, first_flagged_at, last_alerted_at, resolved_at, alert_count

## Casey — Current Status ✅
- Google Sheets connected — reads 61 active jobs
- HubSpot cross-reference — searches deals by client name, gets owner/email
- PM names normalized from sheet abbreviations to full names
- Database tracking — upsert per job, never overwrites schedule if already set
- 7-day update cycle — next_scheduled_update tracked per job
- 14-day escalation — detects stale PM contact, DMs Josh
- Deduplication — confirmed working (second run shows 0 jobs due)
- Slack daily summary to #casey-daily
- Slack update notifications per job
- Anthropic email composition — LLM writes personalized emails
- Gmail sending — confirmed working FROM julie@bluecollarscholars.net (domain-wide delegation active)
- Invalid row handling — skips empty PM, non-PM values like "Construction"

## Casey — Not Done Yet ❌
- Email scenarios (not started / in progress / invoice overdue) — currently one generic template
- CC sales rep on emails
- HubSpot write-back after email sent (needs custom fields + write scope)
- QuickBooks invoice check (missing Realm ID and OAuth refresh token)
- Wire email into main run loop (currently Slack-only notifications)
- Retry logic for intermittent HubSpot network errors (Errno 101)

## Casey Run Flow
1. Load 61 sheet jobs (one Sheets API call, cached)
2. Load all HubSpot owners once (48 owners cached)
3. For each sheet job: search HubSpot by client name → get deal ID, owner, contact email → upsert to DB
4. Process jobs due for update (next_scheduled_update is null or past)
5. Check escalation: if last_pm_contact >= 14 days → DM Josh
6. Send update notification to #casey-daily channel
7. Post daily summary

## OCA — Current Status
- Scaffold exists (agents/oca/main.py, checks.py, alerts.py)
- `oca_flags` table exists in DB
- All 5 check functions are stubs ready to implement
- **OCA has NOT been built yet — this is next**

## OCA — What to Build
5 detection checks (all use Google Sheet as data source):
1. `check_stale_records()` — PM contact > 7 days ago (warning) or > 14 days (urgent)
2. `check_missing_pm()` — no PM assigned, job starting within 14 days (urgent)
3. `check_unconfirmed_crew()` — no contractor assigned, job starting within 7 days (urgent)
4. `check_dropped_invoices()` — balance outstanding, deposit > 30 days ago, no recent contact (warning)
5. `check_job_readiness()` — job in sheet but no matching HubSpot deal (info)

Alert routing (all Slack since WhatsApp dropped):
- urgent → DM Josh + DM PM immediately
- warning → DM PM only (24hr cooldown per flag)
- info → post to #oca-alerts channel
- Sam gets daily digest DM
- Josh gets weekly summary DM every Monday

Deduplication: `should_alert_again(job_id, flag_type, cooldown_hours=24)` prevents hourly spam
Run schedule: every hour (via Railway cron or Docker)

## Key Files
```
integrations/
  hubspot.py      — search_deals_by_client_name(), get_all_owners(), get_contact_email_for_deal()
  sheets.py       — get_active_jobs(), get_jobs_by_client(), parse_latest_date(), normalize_pm_name()
  slack.py        — send_message(), send_dm()
  gmail.py        — send_email() with domain-wide delegation
agents/
  casey/
    main.py       — full run loop, sheet-driven
    email_composer.py — Anthropic API email composition
  oca/
    main.py       — stub
    checks.py     — 5 stub functions
    alerts.py     — stub
db/
  state_store.py  — all DB functions
config/
  pm_config.json  — 10 PMs with emails and Slack IDs
```

## Pending from Client
- HubSpot custom fields (6 fields on Deal object for write-back)
- HubSpot token with write scope
- QuickBooks Realm ID (correct one) + OAuth refresh token
- 2-3 test deals in HubSpot with different PMs assigned
- Confirmation of which deal stages = active jobs

## Known Issues
- Duplicate log lines (Docker issue, mostly fixed)
- Some clients not found in HubSpot (0 deals returned) — expected for some
- Intermittent HubSpot Errno 101 network drops — no retry logic yet
- "Yower" PM name now mapped correctly

## Deployment Plan
- Railway — Docker + managed PostgreSQL
- Casey: daily cron at 8 AM
- OCA: every hour cron
- Both use same Docker image, different entry points
