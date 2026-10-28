"""The connector handshake as claude.ai walks it, signed in through MyAuth."""
import base64
import hashlib
import urllib.parse

from starlette.testclient import TestClient

from garmin_mcp import oauth
from garmin_mcp.access import myauth
from garmin_mcp.server import build_http_app

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "a" * 64
SAME_ORIGIN = {"Origin": "https://garmin.example"}


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _register(client) -> str:
    r = client.post("/oauth/register",
                    json={"client_name": "Claude", "redirect_uris": [REDIRECT]})
    assert r.status_code == 201
    return r.json()["client_id"]


def _params(client_id: str) -> dict:
    return {"response_type": "code", "client_id": client_id,
            "redirect_uri": REDIRECT, "code_challenge": _challenge(VERIFIER),
            "code_challenge_method": "S256", "state": "xyz", "scope": "mcp"}


def _tokens(client) -> tuple[str, dict]:
    client_id = _register(client)
    ok = client.post("/oauth/authorize", data={**_params(client_id), "decision": "allow"},
                     headers=SAME_ORIGIN, follow_redirects=False)
    code = urllib.parse.parse_qs(urllib.parse.urlparse(ok.headers["location"]).query)["code"][0]
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": VERIFIER}).json()
    return client_id, tok


def _refresh(client, client_id, tok):
    return client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        "client_id": client_id})


def test_discovery_documents(client):
    prm = client.get("/.well-known/oauth-protected-resource").json()
    assert prm["resource"] == "https://garmin.example/mcp"
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["code_challenge_methods_supported"] == ["S256"]


def test_unauthorized_mcp_points_at_the_metadata(client):
    r = client.post("/mcp", json={})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]


def test_register_rejects_http_redirect(client):
    r = client.post("/oauth/register", json={"redirect_uris": ["http://evil.test/cb"]})
    assert r.status_code == 400


def test_authorize_without_a_session_goes_to_the_myauth_login(client, user_id):
    client_id = _register(client)
    r = client.get("/oauth/authorize", params=_params(client_id), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith(
        "https://garmin.example:1000/login?next=https%3A%2F%2Fgarmin.example%2Foauth%2Fauthorize")


def test_full_authorization_code_flow(anja, user_id):
    client_id = _register(anja)
    params = _params(client_id)

    page = anja.get("/oauth/authorize", params=params)
    assert page.status_code == 200 and "Signed in as Anja" in page.text

    ok = anja.post("/oauth/authorize", data={**params, "decision": "allow"},
                   headers=SAME_ORIGIN, follow_redirects=False)
    assert ok.status_code == 302
    q = urllib.parse.parse_qs(urllib.parse.urlparse(ok.headers["location"]).query)
    assert q["state"] == ["xyz"]

    tok = anja.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0],
        "redirect_uri": REDIRECT, "client_id": client_id,
        "code_verifier": VERIFIER}).json()
    assert oauth.access_token_user(tok["access_token"]) == user_id

    again = anja.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0],
        "redirect_uri": REDIRECT, "client_id": client_id, "code_verifier": VERIFIER})
    assert again.json()["error"] == "invalid_grant"

    refreshed = _refresh(anja, client_id, tok).json()
    assert oauth.access_token_user(refreshed["access_token"]) == user_id
    assert refreshed["refresh_token"] == tok["refresh_token"]          # the refresh token stays
    assert oauth.access_token_user(tok["access_token"]) == user_id   # valid until it expires


def test_consent_from_another_site_is_refused(anja):
    client_id = _register(anja)
    r = anja.post("/oauth/authorize", data={**_params(client_id), "decision": "allow"},
                  headers={"Origin": "https://garmin.example:1010"}, follow_redirects=False)
    assert r.status_code == 403


def test_wrong_pkce_verifier_is_rejected(anja):
    client_id = _register(anja)
    ok = anja.post("/oauth/authorize", data={**_params(client_id), "decision": "allow"},
                   headers=SAME_ORIGIN, follow_redirects=False)
    code = urllib.parse.parse_qs(
        urllib.parse.urlparse(ok.headers["location"]).query)["code"][0]
    bad = anja.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": "b" * 64})
    assert bad.json()["error"] == "invalid_grant"


def test_deny_redirects_with_access_denied(anja):
    client_id = _register(anja)
    r = anja.post("/oauth/authorize", data={**_params(client_id), "decision": "deny"},
                  headers=SAME_ORIGIN, follow_redirects=False)
    assert "error=access_denied" in r.headers["location"]


def test_no_refresh_once_myauth_revokes_the_app(anja, user_id, fake_myauth):
    client_id, tok = _tokens(anja)
    fake_myauth.allowed.discard(user_id)
    myauth.clear_cache()
    assert _refresh(anja, client_id, tok).json()["error"] == "invalid_grant"


def test_myauth_down_keeps_the_refresh_token(anja, fake_myauth):
    client_id, tok = _tokens(anja)
    fake_myauth.down = True
    myauth.clear_cache()
    assert _refresh(anja, client_id, tok).status_code == 503
    fake_myauth.down = False
    assert "access_token" in _refresh(anja, client_id, tok).json()


def test_registered_clients_survive_a_restart(anja, monkeypatch):
    client_id = _register(anja)
    with TestClient(build_http_app(), base_url="https://garmin.example") as fresh:
        page = fresh.get("/oauth/authorize", params=_params(client_id),
                         follow_redirects=False)
        assert page.status_code == 303        # known client, only the sign-in is missing


def test_split_layout_public_path_and_private_consent(client, monkeypatch):
    """Public OAuth under a path on the public host, consent page on a private one."""
    public = "https://mcp.example/garminconnect"
    monkeypatch.setenv("PUBLIC_URL", public)
    monkeypatch.setenv("MCP_URL", public)
    monkeypatch.setenv("UI_URL", "https://home.example:1030")

    prm = client.get("/.well-known/oauth-protected-resource").json()
    assert prm["resource"] == public and prm["authorization_servers"] == [public]
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == public
    assert asm["token_endpoint"] == f"{public}/oauth/token"
    assert asm["registration_endpoint"] == f"{public}/oauth/register"
    assert asm["authorization_endpoint"] == "https://home.example:1030/oauth/authorize"

    r = client.post("/mcp", json={})
    assert ('resource_metadata="https://mcp.example/.well-known/oauth-protected-resource/'
            'garminconnect"') in r.headers["www-authenticate"]
