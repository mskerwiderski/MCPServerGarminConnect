import sqlite3

import pytest

from garmin_mcp import db, oauth, users
from garmin_mcp.access import myauth


def test_the_same_user_again_keeps_everything(user_id):
    token = oauth._mint_pair("client-1", user_id, "mcp")["access_token"]
    assert users.ensure_user(user_id, "anja", "Anja M.") is False
    assert oauth.access_token_user(token) == user_id
    assert users.get_user(user_id)["display_name"] == "Anja M."


def test_a_reused_uid_does_not_inherit_the_old_data(user_id):
    """A MyAuth database rebuilt from scratch can hand uid 7 to somebody else."""
    token = oauth._mint_pair("client-1", user_id, "mcp")["access_token"]
    assert users.ensure_user(user_id, "eve", "Eve") is True
    assert oauth.access_token_user(token) is None
    assert users.get_user(user_id)["username"] == "eve"


def test_delete_user_removes_everything(user_id):
    oauth._mint_pair("client-1", user_id, "mcp")
    users.delete_user(user_id)
    assert users.get_user(user_id) is None
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM oauth_tokens").fetchone()["n"] == 0


def test_a_database_from_the_account_version_is_refused(tmp_path, monkeypatch):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT)")
    monkeypatch.setenv("MCP_DB", str(old))
    with pytest.raises(RuntimeError, match="account-based"):
        db.init()


def test_the_garmin_tokens_of_0_2_are_dropped(tmp_path, monkeypatch):
    """Since 0.3 MyAuth holds the Garmin connection; the old copy must go."""
    old = tmp_path / "v02.db"
    with sqlite3.connect(old) as c:
        c.execute("CREATE TABLE garmin_tokens (user_id INTEGER PRIMARY KEY, oauth1_enc TEXT)")
    monkeypatch.setenv("MCP_DB", str(old))
    db.init()
    with db.conn() as c:
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='garmin_tokens'"
                             ).fetchall()


def test_missing_app_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("MYAUTH_APP_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MYAUTH_APP_KEY"):
        myauth.require_configured()
