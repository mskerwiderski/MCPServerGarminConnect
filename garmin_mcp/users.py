"""The local side of a MyAuth user.

Who may use this server is decided in MyAuth, not here, and so is the Garmin
connection (MyAuth signs in to Garmin and hands out access tokens). A row in
`users` only exists to hang the OAuth grants and the FIT cache on, and its id
IS the MyAuth uid.

The username is kept for the one case where that could go wrong: a MyAuth
database rebuilt from scratch hands the same uid to somebody else. When the
name behind a uid changes, everything stored for the previous owner is dropped
before the new one can see it - these are other people's health data.
"""

from __future__ import annotations

from .db import conn, now_ms


def ensure_user(uid: int, username: str, display_name: str = "") -> bool:
    """Create or refresh the row for this MyAuth user. True when the uid used
    to belong to a different username and its data was just dropped."""
    with conn() as c:
        row = c.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
    reset = row is not None and row["username"] != username
    if reset:
        from .session import forget_user
        forget_user(uid)
        delete_user(uid)
    with conn() as c:
        if row is None or reset:
            c.execute("INSERT INTO users (id, username, display_name, created_at_ms,"
                      " last_seen_ms) VALUES (?,?,?,?,?)",
                      (uid, username, display_name or "", now_ms(), now_ms()))
        else:
            c.execute("UPDATE users SET display_name=?, last_seen_ms=? WHERE id=?",
                      (display_name or "", now_ms(), uid))
    return reset


def get_user(user_id: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def delete_user(user_id: int) -> None:
    """Everything this server holds for that person: connector grants, the row
    itself. The FIT cache is removed by `session.forget_user`, which knows the
    cache directory. MyAuth - account and Garmin connection - is not touched."""
    with conn() as c:
        c.execute("DELETE FROM oauth_tokens WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
