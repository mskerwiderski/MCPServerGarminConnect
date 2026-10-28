"""Standalone mode: `garmin-mcp login/status/logout/stdio` and LocalSession,
against a fake Garmin sign-in (garmin_login.signin._client) - no network, and
the token file lives in the test's tmp dir (GARMIN_MCP_TOKENS, conftest)."""
import asyncio
import json
import os
import stat
import subprocess
import sys
import time
from urllib.parse import parse_qs

import httpx
import pytest

from garmin_mcp import cli, local
from garmin_mcp.garmin_api.client import GarminError
from garmin_mcp.garmin_login import signin
from garmin_mcp.server import build_server
from garmin_mcp.session import NotConnected


class FakeGarmin:
    """Just enough of sso.garmin.com / connectapi.garmin.com to sign in."""

    def __init__(self):
        self.mfa = True
        self.refuse_refresh = False
        self.exchanges = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sso/embed":
            return httpx.Response(200, text="<html></html>")
        if path == "/sso/signin" and request.method == "GET":
            return httpx.Response(200, text='<input name="_csrf" value="c1">')
        if path == "/sso/signin":
            if parse_qs(request.content.decode())["password"] != ["garmin-pw"]:
                return httpx.Response(200, text="<title>GARMIN Authentication Application</title>")
            if self.mfa:
                return httpx.Response(200, text="<title>MFA Challenge</title>")
            return httpx.Response(200, text='<title>Success</title><a href="embed?ticket=ST-1">')
        if path == "/sso/verifyMFA/loginEnterMfaCode" and request.method == "GET":
            return httpx.Response(200, text='<input name="_csrf" value="c3">')
        if path == "/sso/verifyMFA/loginEnterMfaCode":
            if parse_qs(request.content.decode())["mfa-code"] != ["123456"]:
                return httpx.Response(200, text="<title>MFA Challenge</title>")
            return httpx.Response(200, text='<title>Success</title><a href="embed?ticket=ST-2">')
        if path == "/oauth-service/oauth/preauthorized":
            return httpx.Response(200, text="oauth_token=t1&oauth_token_secret=s1")
        if path == "/oauth-service/oauth/exchange/user/2.0":
            if self.refuse_refresh:
                return httpx.Response(401, text="nope")
            self.exchanges += 1
            return httpx.Response(200, json={"access_token": f"access-{self.exchanges}",
                                             "expires_in": 3600})
        if path == "/userprofile-service/socialProfile":
            return httpx.Response(200, json={"fullName": "Max Muster"})
        return httpx.Response(404)


@pytest.fixture
def garmin(monkeypatch):
    fake = FakeGarmin()
    monkeypatch.setattr(signin, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(fake.handle), follow_redirects=True))
    return fake


def _typing(monkeypatch, *answers, password="garmin-pw"):
    """What the user types at the prompts."""
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(it))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt="": password)


def _tokens(expires_in=3600.0, access="access-x"):
    return {"oauth1": {"oauth_token": "t1", "oauth_token_secret": "s1", "mfa_token": None},
            "oauth2": {"access_token": access, "expires_at": time.time() + expires_in},
            "account": "Max Muster"}


# ---------- login, status, logout ----------

def test_login_with_mfa_writes_a_private_token_file(garmin, monkeypatch, capsys):
    _typing(monkeypatch, "max@example.com", "000000", "123456")
    assert cli.main(["login"]) == 0
    out = capsys.readouterr()
    assert "Signed in as Max Muster" in out.out
    assert "did not accept that code" in out.err             # the first code was wrong
    path = local.token_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    saved = json.loads(path.read_text())
    assert saved["oauth1"]["oauth_token"] == "t1"
    assert saved["oauth2"]["access_token"] == "access-1"
    assert "password" not in path.read_text() and "garmin-pw" not in path.read_text()


def test_a_wrong_password_writes_nothing(garmin, monkeypatch, capsys):
    _typing(monkeypatch, "max@example.com", password="wrong")
    assert cli.main(["login"]) == 1
    assert "Sign-in failed" in capsys.readouterr().err
    assert not local.token_path().exists()


def test_three_wrong_codes_give_up(garmin, monkeypatch):
    _typing(monkeypatch, "max@example.com", "1", "2", "3", "123456")
    assert cli.main(["login"]) == 1
    assert not local.token_path().exists()


def test_status_and_logout(monkeypatch, capsys):
    assert cli.main(["status"]) == 1
    assert "Not signed in" in capsys.readouterr().out
    local.save(_tokens())
    assert cli.main(["status"]) == 0
    assert "Signed in as Max Muster" in capsys.readouterr().out
    assert cli.main(["logout"]) == 0
    assert not local.token_path().exists()
    assert cli.main(["logout"]) == 0
    assert "no tokens" in capsys.readouterr().out


def test_the_default_place_is_the_users_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("GARMIN_MCP_TOKENS")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert local.token_path() == tmp_path / "xdg" / "garmin-mcp" / "tokens.json"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert local.token_path() == tmp_path / "home" / ".config" / "garmin-mcp" / "tokens.json"


# ---------- LocalSession ----------

def test_without_tokens_the_session_says_how_to_sign_in():
    with pytest.raises(NotConnected, match="garmin-mcp login"):
        asyncio.run(local.LocalSession().client())


def test_a_valid_token_is_used_as_is(garmin):
    local.save(_tokens(access="still-good"))
    c = asyncio.run(local.LocalSession().client())
    assert c.oauth2_token.access_token == "still-good"
    assert garmin.exchanges == 0


def test_an_expiring_token_is_renewed_and_saved(garmin):
    local.save(_tokens(expires_in=60))
    c = asyncio.run(local.LocalSession().client())
    assert c.oauth2_token.access_token == "access-1"
    assert local.load()["oauth2"]["access_token"] == "access-1"
    assert garmin.exchanges == 1


def test_a_refused_renewal_asks_to_sign_in_again(garmin):
    garmin.refuse_refresh = True
    local.save(_tokens(expires_in=60))
    with pytest.raises(GarminError, match="garmin-mcp login"):
        asyncio.run(local.LocalSession().client())


def test_a_tool_shows_the_sign_in_hint_to_the_model():
    server = build_server(lambda: local.LocalSession())
    with pytest.raises(Exception, match="garmin-mcp login"):
        asyncio.run(server.call_tool("whoami", {}))


# ---------- stdio, end to end ----------

def test_stdio_speaks_mcp(tmp_path):
    """The real `garmin-mcp stdio` process: initialize, list the tools, and a
    tool call without tokens answers with the sign-in hint."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "whoami", "arguments": {}}},
    ]
    env = {**os.environ, "GARMIN_MCP_TOKENS": str(tmp_path / "none.json"),
           "GARMIN_MCP_CACHE": str(tmp_path / "cache")}
    env.pop("MYAUTH_URL", None)
    proc = subprocess.Popen([sys.executable, "-m", "garmin_mcp.cli", "stdio"], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    answers = {}
    try:
        # Keep stdin open until the last answer is in, like a real client:
        # the server stops when its input ends.
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in msgs))
        proc.stdin.flush()
        deadline = time.time() + 60
        while 3 not in answers and time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if "id" in msg:
                answers[msg["id"]] = msg
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    assert 3 in answers, proc.stderr.read()
    assert answers[1]["result"]["serverInfo"]["name"] == "garmin-connect"
    names = {t["name"] for t in answers[2]["result"]["tools"]}
    assert {"whoami", "list_activities", "get_activity"} <= names
    call = answers[3]["result"]
    assert call["isError"] and "garmin-mcp login" in call["content"][0]["text"]
