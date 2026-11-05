from datetime import datetime, timezone


def utc_now() -> datetime:
    """
    Return naive UTC datetime.
    Ensures seamless compatibility with both SQLite and PostgreSQL
    TIMESTAMP WITHOUT TIME ZONE columns across asyncpg/aiosqlite drivers.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
