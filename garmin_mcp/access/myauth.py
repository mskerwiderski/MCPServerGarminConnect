"""Sign-in and access checks through MyAuth, the self-hosted login service.

The servers using this keep no passwords and no web sessions. Who may use a
server is decided in MyAuth (the app permission `MYAUTH_APP`), and the
questions go there over the internal Docker network (`MYAUTH_URL`, e.g.
http://myauth:8000):

- A browser page: `GET /api/session?app=<key>` with the browser's
  `sso_session` cookie. 200 = signed in and allowed, 401 = not signed in (or a
  password change is pending), 403 = signed in but not allowed here.
  While an admin is signed in as the user, the 200 body carries
  `impersonated_by` ({username, display_name}, else null).
- An MCP call carries only the OAuth bearer the server issued, no cookie:
  `GET /api/internal/access?uid=<id>&app=<key>` answers whether that MyAuth
  user may still use the app. MyAuth answers it only inside the Docker network.

Platform access, each with the app's key (`MYAUTH_APP_KEY`) as bearer:

- Garmin: `GET /api/internal/garmin/token?uid=<id>&app=<key>` hands out the
  user's short-lived Garmin access token. MyAuth is the only place that signs
  in to Garmin (SSO, MFA) and renews tokens; the long-lived OAuth1 token never
  leaves it.
- LibreLinkUp: `GET /api/internal/libre/token?uid=<id>&app=<key>[&renew=1]`
  hands out the user's LibreLinkUp session (region host, token, account id)
  and whose glucose it reads. MyAuth is the only place that knows the
  LibreLinkUp password and signs in; `renew=1` asks it to sign in again
  (LibreLinkUp answered 401). `GET /api/internal/connections` tells the state
  without anything secret.

Answers are cached for CACHE_TTL_S, so blocking a user or revoking the app in
MyAuth takes effect within that time (a Garmin token handed out is valid for
at least ten more minutes). MyAuth unreachable -> `Unavailable`, and the
caller fails closed. `_fetch_session`, `_fetch_access`, `_fetch_garmin`,
`_fetch_libre` and `_fetch_connections` are the mock points of the tests.

Any other internal MyAuth endpoint (an app's notifications, mail, bank or
glucose calls) goes through `call` (async) or `call_sync`: MyAuth's address,
the app's key as bearer, `app=<key>` (and `uid` when given) as query
parameters, a timeout, the JSON body as a dict, and `Unavailable` when MyAuth
does not answer. The domain functions stay in the app; `_request` and
`_request_sync` are the mock points for tests that fake MyAuth at the HTTP
level.

The sign-in page itself is MyAuth's, on the same host at MYAUTH_PUBLIC_PORT.
Cookies do not separate ports, so a MyAuth sign-in is valid there as well.

Each server calls `configure()` once on import: its default app key (used when
`MYAUTH_APP` is not set) and the platform it needs, for the error message of
`require_configured()`.
"""

from __future__ import annotations

import os
import time
from urllib.parse import quote, urlparse

import httpx

SSO_COOKIE = "sso_session"
CACHE_TTL_S = 30
_CACHE_MAX = 500

# cookie / uid -> (valid until, status, body)
_sessions: dict[str, tuple[float, int, dict]] = {}
_access: dict[int, tuple[float, int, dict]] = {}
_garmin: dict[int, tuple[float, int, dict]] = {}
_libre: dict[int, tuple[float, int, dict]] = {}
_connections: dict[int, tuple[float, int, dict]] = {}

_default_app = ""
_platform: str | None = "the platform"
_auth_url = None          # callable or None: then MYAUTH_URL
_public_port = None       # callable or None: then MYAUTH_PUBLIC_PORT


class Unavailable(RuntimeError):
    """MyAuth did not answer."""


class GarminRefused(RuntimeError):
    """MyAuth could not renew the Garmin access; the message says why."""


class LibreRefused(RuntimeError):
    """MyAuth could not sign in to LibreLinkUp; the message says why."""


def configure(app: str, platform: str | None, auth_url=None, public_port=None) -> None:
    """Set the server's default app key (e.g. "mcp-garmin") and the name of
    the platform it reaches through MyAuth (e.g. "Garmin"; None: no platform,
    so no app key is needed). `auth_url` and `public_port` are callables for
    servers whose settings use other names than MYAUTH_URL and
    MYAUTH_PUBLIC_PORT (MyDocContainer: MDC_AUTH_URL, MDC_AUTH_PUBLIC_PORT)."""
    global _default_app, _platform, _auth_url, _public_port
    _default_app, _platform = app, platform
    _auth_url, _public_port = auth_url, public_port


def auth_url() -> str:
    url = _auth_url() if _auth_url else os.environ.get("MYAUTH_URL", "")
    return (url or "").rstrip("/")


def app_key() -> str:
    return os.environ.get("MYAUTH_APP", _default_app)


def app_secret() -> str:
    return os.environ.get("MYAUTH_APP_KEY", "")


def require_configured() -> None:
    """The HTTP server has no other way to know who is asking, and no other
    way to reach the platform."""
    if not auth_url():
        raise RuntimeError("MyAuth's address is not set (MYAUTH_URL, or the server's own "
                           "setting). The HTTP server signs people in through MyAuth, "
                           "e.g. http://myauth:8000.")
    if not app_key():
        raise RuntimeError("MYAUTH_APP is not set and the server set no default "
                           "(access.myauth.configure).")
    if _platform and not app_secret():
        raise RuntimeError("MYAUTH_APP_KEY is not set. It is this app's key in MyAuth's "
                           f"APP_KEYS; without it there is no {_platform} access.")


def public_url(base: str, path: str = "/") -> str:
    """A MyAuth page on the host this server is reached under."""
    port = int(_public_port() if _public_port else os.environ.get("MYAUTH_PUBLIC_PORT", "1000"))
    return f"https://{urlparse(base).hostname}:{port}{path}"


def login_url(base: str, next_url: str, password_change: bool = False) -> str:
    page = "/password" if password_change else "/login"
    return public_url(base, page) + "?next=" + quote(next_url, safe="")


def stop_impersonating_url(base: str, next_url: str) -> str:
    """Ends an admin's "sign in as" in MyAuth; MyAuth sends them back to
    `next_url` as themselves."""
    return public_url(base, "/impersonate/stop") + "?next=" + quote(next_url, safe="")


def _body(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def _fetch_session(cookie: str) -> tuple[int, dict]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{auth_url()}/api/session", params={"app": app_key()},
                                 headers={"cookie": f"{SSO_COOKIE}={cookie}"})
    except httpx.HTTPError as exc:
        raise Unavailable("MyAuth is not reachable") from exc
    return r.status_code, _body(r)


async def _fetch_access(uid: int) -> tuple[int, dict]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{auth_url()}/api/internal/access",
                                 params={"uid": uid, "app": app_key()})
    except httpx.HTTPError as exc:
        raise Unavailable("MyAuth is not reachable") from exc
    return r.status_code, _body(r)


def _query(uid: int | None, params: dict | None) -> dict:
    return {"app": app_key(), **({"uid": uid} if uid is not None else {}), **(params or {})}


def _auth_header() -> dict:
    return {"authorization": f"Bearer {app_secret()}"}


async def _request(method: str, url: str, params: dict, data: dict | None,
                   files: list | None, timeout: float) -> tuple[int, dict]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request(method, url, params=params, data=data, files=files,
                                     headers=_auth_header())
    except httpx.HTTPError as exc:
        raise Unavailable("MyAuth is not reachable") from exc
    return r.status_code, _body(r)


def _request_sync(method: str, url: str, params: dict, data: dict | None,
                  files: list | None, timeout: float) -> tuple[int, dict]:
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.request(method, url, params=params, data=data, files=files,
                               headers=_auth_header())
    except httpx.HTTPError as exc:
        raise Unavailable("MyAuth is not reachable") from exc
    return r.status_code, _body(r)


async def call(method: str, path: str, *, uid: int | None = None, params: dict | None = None,
               data: dict | None = None, files: list | None = None,
               timeout: float = 10) -> tuple[int, dict]:
    """(status, JSON body as dict) of an internal MyAuth endpoint, called with
    this app's key as bearer and `app=<key>` (plus `uid` when given) in the
    query. `data`/`files` go as a form (multipart with files, httpx style).
    Raises `Unavailable` when MyAuth does not answer; every HTTP status is
    returned - mapping it to the app's messages is the caller's job."""
    return await _request(method, f"{auth_url()}{path}", _query(uid, params), data, files,
                          timeout)


def call_sync(method: str, path: str, *, uid: int | None = None, params: dict | None = None,
              data: dict | None = None, files: list | None = None,
              timeout: float = 10) -> tuple[int, dict]:
    """`call` for synchronous code (e.g. stores asked from sync paths)."""
    return _request_sync(method, f"{auth_url()}{path}", _query(uid, params), data, files,
                         timeout)


async def _fetch_app(path: str, uid: int, **params) -> tuple[int, dict]:
    """GET an internal MyAuth endpoint with this app's key as bearer."""
    return await call("GET", path, uid=uid, params=params, timeout=30)


async def _fetch_garmin(uid: int) -> tuple[int, dict]:
    return await _fetch_app("/api/internal/garmin/token", uid)


async def _fetch_libre(uid: int, renew: bool = False) -> tuple[int, dict]:
    return await _fetch_app("/api/internal/libre/token", uid, **({"renew": 1} if renew else {}))


async def _fetch_connections(uid: int) -> tuple[int, dict]:
    return await _fetch_app("/api/internal/connections", uid)


async def _cached(cache: dict, key, fetch) -> tuple[int, dict]:
    now = time.monotonic()
    hit = cache.get(key)
    if hit and hit[0] > now:
        return hit[1], hit[2]
    status, body = await fetch(key)          # Unavailable is never cached
    if len(cache) >= _CACHE_MAX:
        cache.clear()
    cache[key] = (now + CACHE_TTL_S, status, body)
    return status, body


async def session_of(cookie: str | None) -> tuple[int, dict]:
    """(status, body) of MyAuth's /api/session for this browser cookie."""
    if not cookie:
        return 401, {"detail": "not_authenticated"}
    return await _cached(_sessions, cookie, _fetch_session)


async def access_of(uid: int) -> dict | None:
    """MyAuth's user record while `uid` may use this app, else None."""
    status, body = await _cached(_access, uid, _fetch_access)
    return body if status == 200 else None


async def garmin_token(uid: int) -> dict | None:
    """{access_token, expires_at, account} of the user's Garmin connection in
    MyAuth, None when they have not connected Garmin there."""
    status, body = await _cached(_garmin, uid, _fetch_garmin)
    if status == 200 and body.get("access_token"):
        return body
    if status == 404:
        return None
    _garmin.pop(uid, None)                       # errors are not worth keeping
    if status == 502:
        raise GarminRefused(str(body.get("message") or "Garmin refused the renewal"))
    raise Unavailable(f"MyAuth answered {status} ({body.get('detail', '')})")


async def libre_session(uid: int, renew: bool = False) -> dict | None:
    """{base_url, token, expires, account_id, patient_id, patient_name, account}
    of the user's LibreLinkUp connection in MyAuth, None when they have not
    connected it there."""
    if renew:
        _libre.pop(uid, None)
        status, body = await _fetch_libre(uid, renew=True)
    else:
        status, body = await _cached(_libre, uid, _fetch_libre)
    if status == 200 and body.get("token") and body.get("patient_id"):
        if renew:
            _libre[uid] = (time.monotonic() + CACHE_TTL_S, status, body)
        return body
    _libre.pop(uid, None)                        # only a session is worth keeping
    if status == 404:
        return None
    if status == 502:
        raise LibreRefused(str(body.get("message") or "LibreLinkUp refused the sign-in"))
    raise Unavailable(f"MyAuth answered {status} ({body.get('detail', '')})")


async def libre_state(uid: int) -> dict:
    """{connected, account, last_error} of the LibreLinkUp connection in MyAuth,
    nothing secret - for the account page and whoami."""
    status, body = await _cached(_connections, uid, _fetch_connections)
    if status != 200:
        _connections.pop(uid, None)
        raise Unavailable(f"MyAuth answered {status} ({body.get('detail', '')})")
    state = body.get("libre")
    return state if isinstance(state, dict) else {"connected": False}


def clear_cache() -> None:
    _sessions.clear()
    _access.clear()
    _garmin.clear()
    _libre.clear()
    _connections.clear()
