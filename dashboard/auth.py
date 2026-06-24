"""Password hashing and cookie-session helpers for the dashboard.

Sessions are stored in the dashboard_sessions DB table (not signed cookies),
so they survive server restarts and can be revoked server-side via logout.
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request

from db.state_store import (
    get_user_by_email, get_user_by_id,
    create_session, get_session, delete_session,
)

COOKIE_NAME = "bcs_session"
SESSION_TTL_DAYS = 30
_PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        iterations_str, salt_hex, hash_hex = stored_hash.split("$")
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_str))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def authenticate(email: str, password: str) -> "dict | None":
    user = get_user_by_email(email)
    if not user or not verify_password(password, user["password_hash"]):
        return None
    return user


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    create_session(user_id, token, expires_at)
    return token


def end_session(token: "str | None") -> None:
    if token:
        delete_session(token)


def get_current_user(request: Request) -> "dict | None":
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    session = get_session(token)
    if not session:
        return None
    expires_at = session["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        delete_session(token)
        return None
    return get_user_by_id(session["user_id"])
