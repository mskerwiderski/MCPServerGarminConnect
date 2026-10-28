"""SQLite storage. One connection per operation, no ORM.

Starlette runs sync endpoints in a threadpool, so a shared connection would be
used across threads. Opening per call sidesteps that entirely and costs
nothing at this scale - this database serves a handful of MyAuth users.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
-- One row per MyAuth user who has used this server; id IS the MyAuth uid.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER
);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id     TEXT PRIMARY KEY,
    client_name   TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    access_token          TEXT PRIMARY KEY,
    refresh_token         TEXT NOT NULL,
    client_id             TEXT NOT NULL,
    user_id               INTEGER NOT NULL,
    scope                 TEXT,
    access_expires_at_ms  INTEGER NOT NULL,
    refresh_expires_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_oauth_tokens_refresh ON oauth_tokens(refresh_token);
CREATE INDEX IF NOT EXISTS ix_oauth_tokens_user ON oauth_tokens(user_id);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def db_path() -> Path:
    env = os.environ.get("MCP_DB")
    return Path(env).expanduser() if env else Path.home() / ".garmin-mcp" / "app.db"


@contextmanager
def conn():
    """Careful: an exception passing through the `yield` skips the commit, so
    writes that must survive a failure have to be committed before the
    exception is raised.
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        _refuse_old_accounts(c)
        c.executescript(SCHEMA)
        # Up to 0.2 the Garmin tokens lived here; since 0.3 MyAuth holds them.
        c.execute("DROP TABLE IF EXISTS garmin_tokens")


def _refuse_old_accounts(c) -> None:
    """Up to 0.1 this server had its own e-mail/password accounts. Their ids
    have nothing to do with MyAuth uids, so such a database must not be reused:
    its Garmin tokens would land with whoever got the same number in MyAuth."""
    columns = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
    if "email" in columns:
        raise RuntimeError(f"{db_path()} comes from the account-based version (0.1). "
                           "Accounts now come from MyAuth - start with an empty MCP_DB.")
