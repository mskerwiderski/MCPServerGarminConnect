"""The HTTP transport as a remote client sees it, and the tenancy boundary."""
from garmin_mcp import oauth, users
from garmin_mcp.access import myauth

HEADERS = {"Accept": "application/json, text/event-stream",
           "Content-Type": "application/json",
           "MCP-Protocol-Version": "2025-06-18"}

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}}}


def _auth(token):
    return {**HEADERS, "Authorization": f"Bearer {token}"}


def _token_for(user_id: int) -> str:
    return oauth._mint_pair("client-1", user_id, "mcp")["access_token"]


def test_health(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_no_token_is_401(client):
    assert client.post("/mcp", json=INIT, headers=HEADERS).status_code == 401


def test_unknown_token_is_401(client, user_id):
    assert client.post("/mcp", json=INIT, headers=_auth("made-up")).status_code == 401


def test_oauth_token_initializes(client, user_id):
    r = client.post("/mcp", json=INIT, headers=_auth(_token_for(user_id)))
    assert r.status_code == 200
    assert r.json()["result"]["serverInfo"]["name"] == "garmin-connect"


def test_tools_list_over_http(client, user_id):
    token = _token_for(user_id)
    client.post("/mcp", json=INIT, headers=_auth(token))
    r = client.post("/mcp", headers=_auth(token),
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"list_activities", "get_daily_health", "get_challenge"} <= names


def test_a_user_without_garmin_gets_a_useful_error(client, user_id):
    token = _token_for(user_id)
    client.post("/mcp", json=INIT, headers=_auth(token))
    r = client.post("/mcp", headers=_auth(token), json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "whoami", "arguments": {}}})
    assert "not connected yet" in str(r.json())


def test_revoking_the_app_in_myauth_cuts_the_connector_off(client, user_id, fake_myauth):
    token = _token_for(user_id)
    assert client.post("/mcp", json=INIT, headers=_auth(token)).status_code == 200
    fake_myauth.allowed.discard(user_id)
    myauth.clear_cache()
    assert client.post("/mcp", json=INIT, headers=_auth(token)).status_code == 401


def test_myauth_down_is_a_503(client, user_id, fake_myauth):
    fake_myauth.down = True
    assert client.post("/mcp", json=INIT, headers=_auth(_token_for(user_id))).status_code == 503


def test_a_reassigned_uid_loses_the_old_token(client, user_id, fake_myauth):
    token = _token_for(user_id)
    fake_myauth.users[user_id]["username"] = "eve"
    assert client.post("/mcp", json=INIT, headers=_auth(token)).status_code == 401
    assert oauth.access_token_user(token) is None


def _whoami(client, uid) -> str:
    token = _token_for(uid)
    client.post("/mcp", json=INIT, headers=_auth(token))
    r = client.post("/mcp", headers=_auth(token), json={
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "whoami", "arguments": {}}})
    return str(r.json())


def test_two_accounts_never_see_each_other(client, user_id, fake_myauth, monkeypatch):
    """The tenancy boundary: the same process, two bearers, two Garmin
    connections from MyAuth - each call reaches Garmin with its own token."""
    other = fake_myauth.add(8, "bob", "Bob")
    users.ensure_user(other, "bob", "Bob")
    fake_myauth.connect_garmin(user_id, "anja-access", "Anja")
    fake_myauth.connect_garmin(other, "bob-access", "Bob")
    from garmin_mcp.garmin_api.client import GarminClient

    async def display_name(self):
        return {"anja-access": "Anja on Garmin", "bob-access": "Bob on Garmin"}[
            self.oauth2_token.access_token]
    monkeypatch.setattr(GarminClient, "fetch_display_name", display_name)

    assert "Anja on Garmin" in _whoami(client, user_id)
    assert "Bob on Garmin" in _whoami(client, other)


def test_a_disconnect_in_myauth_takes_effect(client, user_id, fake_myauth, monkeypatch):
    fake_myauth.connect_garmin(user_id, "anja-access", "Anja")
    from garmin_mcp.garmin_api.client import GarminClient

    async def display_name(self):
        return "Anja on Garmin"
    monkeypatch.setattr(GarminClient, "fetch_display_name", display_name)
    assert "Anja on Garmin" in _whoami(client, user_id)
    fake_myauth.garmin.clear()
    myauth.clear_cache()
    assert "not connected yet" in _whoami(client, user_id)


def test_a_refused_renewal_is_readable(client, user_id, fake_myauth):
    fake_myauth.garmin_refused = "Garmin did not renew the access (HTTP 401)."
    assert "Connect Garmin again on your MyAuth account page" in _whoami(client, user_id)


def test_today_is_the_users_day(monkeypatch):
    """Kiritimati (UTC+14) and Pago Pago (UTC-11) are never on the same date."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from garmin_mcp import session as sessions
    from garmin_mcp.tools import _day
    for zone in ("Pacific/Kiritimati", "Pacific/Pago_Pago"):
        token = sessions.CURRENT_TZ.set(zone)
        try:
            assert _day(None) == datetime.now(ZoneInfo(zone)).date().isoformat()
        finally:
            sessions.CURRENT_TZ.reset(token)
