"""Sign in to Garmin Connect (SSO, MFA) and renew the access token.

Only Garmin's protocol, nothing about storage or users: the caller keeps the
tokens (MyAuth encrypted in its database, Garmin MCP's standalone mode in a
file only its user can read). Taken from MyAuth `app/garmin.py`, which took it
from Garmin MCP / MyFITContainer `garmin_client.py`.

Flow as in garth 0.4.47:
  1) GET  /sso/embed                 collect cookies
  2) GET  /sso/signin                _csrf from the HTML
  3) POST /sso/signin                form sign-in
  4) if asked: POST /sso/verifyMFA/loginEnterMfaCode
  5) ticket from the success page
  6) GET  /oauth-service/oauth/preauthorized     ticket -> OAuth1 (lasts ~1 year)
  7) POST /oauth-service/oauth/exchange/user/2.0 OAuth1 -> OAuth2 (short-lived)

Usage:

    with SignIn() as s:
        tokens = s.start(email, password)        # None = Garmin wants the MFA code
        if tokens is None:
            tokens = s.submit_mfa(code)          # wrong code: GarminError, may retry
    # tokens = {"oauth1": {...}, "oauth2": {...}, "account": "Full Name"}

    if needs_renewal(tokens["oauth2"]):
        tokens["oauth2"] = renew(tokens["oauth1"])

The password is never stored. Sign-ins run one after the other (a module-wide
lock): several in the same second is exactly the pattern Garmin blocks an
address for. This does not work for LiveTrack (live.garmin.com), which only
takes tickets from Garmin's new portal behind a Cloudflare JS check.

`_client` is the mock point of the tests (an httpx transport).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import threading
import time
import uuid
from urllib.parse import unquote

import httpx

# Garmin Connect Mobile's own OAuth1 consumer: not a secret of this project,
# every Garmin community library carries the same two constants (they are in
# Garmin's app). Without them there is no OAuth1 -> OAuth2 exchange.
CONSUMER_KEY = "fc3e99d2-118c-44b8-8ae3-03370dde24c0"
CONSUMER_SECRET = "E08WAR897WEy2knn7aFBrvegVAf0AFdWBBF"
UA_DEFAULT = "GCM-iOS-5.7.2.1"
UA_OAUTH = "com.garmin.android.apps.connectmobile"
SSO_BASE = "https://sso.garmin.com"
CONNECT_API = "https://connectapi.garmin.com"

REFRESH_MARGIN_S = 10 * 60       # a token handed out must hold at least this long


class GarminError(RuntimeError):
    """A message for the user (English, like Garmin's own)."""


# ---------- OAuth1 signature (RFC 5849, HMAC-SHA1) ----------

def _pct(s: str) -> str:
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        out.append(c if c.isalnum() or c in "-._~" else f"%{b:02X}")
    return "".join(out)


def _oauth1_header(method: str, url: str, query: dict, body: dict,
                   token: str | None = None, token_secret: str | None = None) -> str:
    params = {"oauth_consumer_key": CONSUMER_KEY, "oauth_nonce": uuid.uuid4().hex,
              "oauth_signature_method": "HMAC-SHA1",
              "oauth_timestamp": str(int(time.time())), "oauth_version": "1.0"}
    if token:
        params["oauth_token"] = token
    signed = "&".join(f"{_pct(k)}={_pct(v)}"
                      for k, v in sorted({**params, **query, **body}.items()))
    base = f"{method.upper()}&{_pct(url)}&{_pct(signed)}"
    key = f"{_pct(CONSUMER_SECRET)}&{_pct(token_secret or '')}"
    params["oauth_signature"] = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(params.items()))


def _form(data: dict) -> str:
    return "&".join(f"{_pct(k)}={_pct(v)}" for k, v in data.items())


_CSRF_RE = re.compile(r'name="_csrf"\s+value="([^"]+)"')
_TICKET_RE = re.compile(r'embed\?ticket=([^"]+)"')
_TITLE_RE = re.compile(r"<title>([^<]+)</title>")
_MFA_MARKS = ("<title>MFA", "MFA Challenge", "mfa-code", "verifyMFA",
              "loginEnterMfaCode", "setupEnterMfaCode")


def _csrf(html: str) -> str:
    m = _CSRF_RE.search(html)
    return m.group(1) if m else ""


def _ticket(html: str) -> str | None:
    m = _TICKET_RE.search(html)
    return m.group(1) if m else None


def _title(r: httpx.Response) -> str:
    m = _TITLE_RE.search(r.text)
    return m.group(1) if m else f"HTTP {r.status_code}"


def _friendly(text: str) -> str:
    if "429" in text or "403" in text or "cloudflare" in text.lower():
        return ("Garmin refused the sign-in from this address (rate limit or bot "
                "protection). Wait a few minutes and try again.")
    return f"Garmin sign-in failed: {text[:200]}"


# ---------- HTTP (mock point of the tests) ----------

def _client() -> httpx.Client:
    return httpx.Client(timeout=30.0, follow_redirects=True)


# ---------- sign-in ----------

_sso_lock = threading.Lock()


class SignIn:
    """One sign-in attempt. Holds Garmin's cookies between the password and
    the MFA code; `close()` (or the `with` block) ends it."""

    def __init__(self) -> None:
        self._http = _client()
        self._csrf = ""
        self._params = ""
        self.started = time.time()
        self.waiting_for_mfa = False

    def __enter__(self) -> "SignIn":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def start(self, email: str, password: str) -> dict | None:
        """Tokens when signed in, None when Garmin wants the MFA code first."""
        if not email.strip() or not password:
            raise GarminError("Enter your Garmin e-mail and password.")
        embed = f"id=gauth-widget&embedWidget=true&gauthHost={SSO_BASE}/sso"
        self._params = (
            f"{embed}&gauthHost={SSO_BASE}/sso/embed&service={SSO_BASE}/sso/embed"
            f"&source={SSO_BASE}/sso/embed&redirectAfterAccountLoginUrl={SSO_BASE}/sso/embed"
            f"&redirectAfterAccountCreationUrl={SSO_BASE}/sso/embed")
        http = self._http
        try:
            with _sso_lock:
                embed_url = f"{SSO_BASE}/sso/embed?{embed}"
                http.get(embed_url, headers={"User-Agent": UA_DEFAULT})
                signin = f"{SSO_BASE}/sso/signin?{self._params}"
                r = http.get(signin, headers={"User-Agent": UA_DEFAULT, "Referer": embed_url})
                csrf = _csrf(r.text)
                if not csrf:
                    raise GarminError(_friendly(f"no sign-in form ({_title(r)})"))
                r = http.post(signin, content=_form({
                    "username": email.strip(), "password": password, "embed": "true",
                    "_csrf": csrf}),
                    headers={"User-Agent": UA_DEFAULT, "Referer": signin,
                             "Content-Type": "application/x-www-form-urlencoded"})
        except httpx.HTTPError as e:
            raise GarminError(_friendly(f"network: {e}")) from e
        if any(m in r.text for m in _MFA_MARKS):
            self._csrf = _csrf(r.text) or csrf
            self.waiting_for_mfa = True
            return None
        if r.status_code == 429:
            raise GarminError(_friendly("429"))
        ticket = _ticket(r.text)
        if "<title>Success" not in r.text or not ticket:
            raise GarminError(_friendly(_title(r)))
        return self._finish(ticket)

    def submit_mfa(self, code: str) -> dict:
        """Tokens once Garmin accepts the code. A wrong or stale code raises
        GarminError and keeps the sign-in open for the next code; any other
        failure ends it (`waiting_for_mfa` is False then)."""
        if not self.waiting_for_mfa:
            raise GarminError("The sign-in timed out. Please start again.")
        url = f"{SSO_BASE}/sso/verifyMFA/loginEnterMfaCode?{self._params}"
        http = self._http
        try:
            with _sso_lock:
                r = http.get(url, headers={"User-Agent": UA_DEFAULT})
                self._csrf = _csrf(r.text) or self._csrf
                r = http.post(url, content=_form({
                    "mfa-code": code.strip(), "embed": "true", "_csrf": self._csrf,
                    "fromPage": "setupEnterMfaCode"}),
                    headers={"User-Agent": UA_DEFAULT, "Referer": url,
                             "Content-Type": "application/x-www-form-urlencoded"})
        except httpx.HTTPError as e:
            self.waiting_for_mfa = False
            raise GarminError(_friendly(f"network: {e}")) from e
        ticket = _ticket(r.text)
        if "<title>Success" not in r.text or not ticket:
            if any(m in r.text for m in _MFA_MARKS):
                # Garmin shows its code page again: the code was wrong or stale
                # (every sign-in attempt sends a new one). The sign-in stays open.
                self._csrf = _csrf(r.text) or self._csrf
                raise GarminError("Garmin did not accept that code. Enter the newest code "
                                  "Garmin sent you - each attempt sends a new one, and codes "
                                  "expire after a few minutes.")
            self.waiting_for_mfa = False
            raise GarminError(f"{_friendly(_title(r))} Please start again.")
        self.waiting_for_mfa = False
        return self._finish(ticket)

    def _finish(self, ticket: str) -> dict:
        """Ticket -> OAuth1 -> OAuth2, plus the account's display name."""
        http = self._http
        url = f"{CONNECT_API}/oauth-service/oauth/preauthorized"
        query = {"ticket": ticket, "login-url": f"{SSO_BASE}/sso/embed",
                 "accepts-mfa-tokens": "true"}
        try:
            r = http.get(f"{url}?{_form(dict(sorted(query.items())))}", headers={
                "Authorization": _oauth1_header("GET", url, query, {}), "User-Agent": UA_OAUTH})
            parts = {unquote(k): unquote(v) for k, _, v in
                     (p.partition("=") for p in r.text.split("&") if "=" in p)}
            if "oauth_token" not in parts or "oauth_token_secret" not in parts:
                raise GarminError(f"Garmin did not hand out tokens (HTTP {r.status_code}).")
            oauth1 = {"oauth_token": parts["oauth_token"],
                      "oauth_token_secret": parts["oauth_token_secret"],
                      "mfa_token": parts.get("mfa_token")}
            oauth2 = _exchange(http, oauth1)
        except httpx.HTTPError as e:
            raise GarminError(_friendly(f"network: {e}")) from e
        return {"oauth1": oauth1, "oauth2": oauth2, "account": _display_name(http, oauth2)}


# ---------- renewal ----------

def _exchange(http: httpx.Client, oauth1: dict) -> dict:
    url = f"{CONNECT_API}/oauth-service/oauth/exchange/user/2.0"
    body = {"mfa_token": oauth1["mfa_token"]} if oauth1.get("mfa_token") else {}
    r = http.post(url, content=_form(body), headers={
        "Authorization": _oauth1_header("POST", url, {}, body, oauth1["oauth_token"],
                                        oauth1["oauth_token_secret"]),
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA_OAUTH})
    if r.status_code != 200:
        raise GarminError(f"Garmin did not renew the access (HTTP {r.status_code}).")
    j = r.json()
    return {"access_token": j["access_token"], "refresh_token": j.get("refresh_token", ""),
            "expires_in": int(j["expires_in"]), "expires_at": time.time() + float(j["expires_in"])}


def needs_renewal(oauth2: dict | None, margin_s: float = REFRESH_MARGIN_S) -> bool:
    """True when there is no access token or it expires within `margin_s`."""
    return oauth2 is None or float(oauth2.get("expires_at", 0)) - margin_s < time.time()


def renew(oauth1: dict) -> dict:
    """A fresh OAuth2 access token from the OAuth1 token. GarminError when
    Garmin refuses (the OAuth1 token is revoked or expired: sign in again)."""
    try:
        with _client() as http:
            return _exchange(http, oauth1)
    except httpx.HTTPError as e:
        raise GarminError(_friendly(f"network: {e}")) from e


def _display_name(http: httpx.Client, oauth2: dict) -> str:
    try:
        r = http.get(f"{CONNECT_API}/userprofile-service/socialProfile", headers={
            "Authorization": f"Bearer {oauth2['access_token']}", "nk": "NT",
            "User-Agent": UA_DEFAULT})
        j = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return ""
    return j.get("fullName") or j.get("displayName") or j.get("userName") or ""
