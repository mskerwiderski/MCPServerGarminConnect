"""The account page, with MyAuth deciding who is signed in."""
from garmin_mcp import oauth, users
from garmin_mcp.access import myauth


def test_account_without_a_session_goes_to_the_myauth_login(client, user_id):
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == ("https://garmin.example:1000/login"
                                     "?next=https%3A%2F%2Fgarmin.example%2Faccount")


def test_a_pending_password_change_goes_to_myauth_first(client, user_id, monkeypatch,
                                                        fake_myauth):
    async def must_change(cookie):
        return 401, {"detail": "password_change_required"}
    monkeypatch.setattr(myauth, "_fetch_session", must_change)
    client.cookies.set("sso_session", "anja-cookie")
    r = client.get("/account", follow_redirects=False)
    assert r.headers["location"].startswith("https://garmin.example:1000/password?next=")


def test_account_page(anja):
    page = anja.get("/account").text
    assert "Anja" in page and "not connected" in page
    assert 'href="https://garmin.example:1000/connections#garmin">Connect Garmin in MyAuth' in page
    assert ">https://garmin.example/mcp</textarea>" in page and "Copy address" in page
    assert "https://garmin.example:1000/logout?next=/login" in page
    assert "/static/ui/tokens.css?v=" in page



def test_the_footer_shows_the_version_and_an_optional_note(anja, monkeypatch):
    from garmin_mcp import __version__
    assert f"<footer>Garmin MCP v{__version__}</footer>" in anja.get("/account").text
    monkeypatch.setenv("FOOTER_NOTE", "Private service <test>")
    assert f"v{__version__} · Private service &lt;test&gt;</footer>" in anja.get("/account").text


def test_msk_ui_is_served(client):
    r = client.get("/static/ui/tokens.css")
    assert r.status_code == 200 and "msk-ui v" in r.text


def test_a_user_without_the_app_gets_no_access(client, fake_myauth):
    fake_myauth.add(8, "bob", cookie="bob-cookie", allowed=False)
    client.cookies.set("sso_session", "bob-cookie")
    r = client.get("/account")
    assert r.status_code == 403 and "not enabled for Garmin MCP" in r.text
    assert users.get_user(8) is None


def test_myauth_down_is_a_503(anja, fake_myauth):
    fake_myauth.down = True
    r = anja.get("/account")
    assert r.status_code == 503 and "not answering" in r.text


def test_the_garmin_connection_is_shown_from_myauth(anja, user_id, fake_myauth):
    fake_myauth.connect_garmin(user_id, "anja-access", "Anja Muster")
    page = anja.get("/account").text
    assert "connected</span> as <b>Anja Muster</b>" in page and "MyAuth account page" in page
    fake_myauth.garmin_refused = "Garmin did not renew the access (HTTP 401)."
    myauth.clear_cache()
    assert "HTTP 401" in anja.get("/account").text


def test_a_form_from_another_site_is_refused(anja, user_id):
    token = oauth._mint_pair("client-1", user_id, "mcp")["access_token"]
    r = anja.post("/account/delete", headers={"Origin": "https://garmin.example:1010"})
    assert r.status_code == 403
    assert oauth.access_token_user(token) == user_id


def test_delete_my_data(anja, user_id):
    token = oauth._mint_pair("client-1", user_id, "mcp")["access_token"]
    r = anja.post("/account/delete", headers={"Origin": "https://garmin.example"})
    assert r.status_code == 200 and "not connected" in r.text     # still signed in
    assert oauth.access_token_user(token) is None


def test_the_ui_uses_the_host_the_browser_came_through(client, user_id, monkeypatch):
    monkeypatch.setenv("MCP_URL", "https://mcp.example/garminconnect")
    ui = {"host": "home.example:1030"}
    r = client.get("/account", headers=ui, follow_redirects=False)
    assert r.headers["location"] == ("https://home.example:1000/login"
                                     "?next=https%3A%2F%2Fhome.example%3A1030%2Faccount")
    client.cookies.set("sso_session", "anja-cookie")
    assert ">https://mcp.example/garminconnect</textarea>" in client.get("/account", headers=ui).text
    ok = client.post("/account/delete", follow_redirects=False,
                     headers={**ui, "origin": "https://home.example:1030"})
    assert ok.status_code == 303


def test_banner_when_an_admin_is_signed_in_as_the_user(anja, user_id, fake_myauth):
    fake_myauth.users[user_id]["impersonated_by"] = {"username": "max",
                                                     "display_name": "Max"}
    page = anja.get("/account").text
    assert "imp-banner" in page and "signed in as Anja - on behalf of Max" in page
    assert ("https://garmin.example:1000/impersonate/stop?next="
            "https%3A%2F%2Fgarmin.example%2Faccount") in page
    # the consent page too: a connector allowed now would be Anja's
    client_id = anja.post("/oauth/register", json={
        "client_name": "Claude",
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}).json()["client_id"]
    consent = anja.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id,
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_challenge": "a" * 43, "code_challenge_method": "S256"}).text
    assert "imp-banner" in consent and "on behalf of Max" in consent
    del fake_myauth.users[user_id]["impersonated_by"]
    myauth.clear_cache()
    assert "imp-banner" not in anja.get("/account").text


def test_an_unexpected_myauth_answer_fails_closed(client, user_id, monkeypatch):
    """Since access.web: anything but 200/401/403 from MyAuth is "sign-in
    unavailable", not a redirect to the sign-in page."""
    async def broken(cookie):
        return 500, {}
    monkeypatch.setattr(myauth, "_fetch_session", broken)
    myauth.clear_cache()
    client.cookies.set("sso_session", "anja-cookie")
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 503 and "Sign-in unavailable" in r.text
