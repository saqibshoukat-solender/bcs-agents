def cfg(key: str, _env_fallback: str = None) -> str:
    """Read credential from DB only. Returns '' if not configured."""
    try:
        from db.state_store import get_config
        return get_config(key) or ""
    except Exception:
        return ""
