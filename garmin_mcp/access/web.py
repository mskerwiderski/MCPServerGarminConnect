"""Browser sign-in for web apps behind MyAuth.

A web app asks MyAuth on each request who the browser belongs to: the
`sso_session` cookie goes to `GET /api/session?app=<key>` (through
`myauth.session_of`, cached 30 s, `Unavailable` never cached). `check` turns
MyAuth's answer into one of five outcomes; the app's middleware (or FastAPI
dependency) turns the outcome into its own response - its pages, its
language, its admin or owner rules stay in the app.

  OK               signed in and allowed; `Gate.user` is MyAuth's user record
  SIGN_IN          no cookie, or MyAuth answered 401
  CHANGE_PASSWORD  401 with detail `password_change_required`
  NO_ACCESS        403: signed in, but the app is not enabled for the user
  UNAVAILABLE      MyAuth unreachable or an unexpected answer (fail closed)

MyAuth's public pages are on the host the browser used, at MyAuth's public
port (`myauth.public_url`); cookies do not separate ports, so a sign-in there
is valid here. The helpers take any Starlette/FastAPI request (they only read
`request.headers`, `request.url` and `request.cookies`), so this module
imports neither framework.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import myauth

OK = "ok"
SIGN_IN = "sign_in"
CHANGE_PASSWORD = "change_password"
NO_ACCESS = "no_access"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Gate:
    """What `check` found out; `user` only with OK."""

    outcome: str
    user: dict | None = None


def current_url(request) -> str:
    """The URL the browser asked for, as https on the host it used."""
    query = f"?{request.url.query}" if request.url.query else ""
    return f"https://{request.headers.get('host', request.url.netloc)}{request.url.path}{query}"


def myauth_page(request, path: str = "/") -> str:
    """A MyAuth page on the host the browser used (private name or LAN IP)."""
    return myauth.public_url(current_url(request), path)


def login_target(request, password_change: bool = False) -> str:
    """Where to send the browser to sign in (or to change its password first),
    coming back to the page it asked for."""
    here = current_url(request)
    return myauth.login_url(here, here, password_change)


async def check(request) -> Gate:
    """Who the browser belongs to, according to MyAuth."""
    cookie = request.cookies.get(myauth.SSO_COOKIE)
    if not cookie:
        return Gate(SIGN_IN)
    try:
        status, body = await myauth.session_of(cookie)
    except myauth.Unavailable:
        return Gate(UNAVAILABLE)
    if status == 200:
        return Gate(OK, body)
    if status == 403:
        return Gate(NO_ACCESS)
    if status == 401:
        password = body.get("detail") == "password_change_required"
        return Gate(CHANGE_PASSWORD if password else SIGN_IN)
    return Gate(UNAVAILABLE)


def account(request, user: dict | None) -> dict | None:
    """Header data for the signed-in user (None without one): the name, links
    to the MyAuth account and log-out, and - while an admin acts as this user
    ("sign in as" in MyAuth) - who that admin is and the link that ends it."""
    if not user:
        return None
    imp = user.get("impersonated_by")
    here = current_url(request)
    return {"name": user.get("display_name") or user.get("username"),
            "account_url": myauth_page(request),
            "logout_url": myauth_page(request, "/logout?next=/login"),
            "impersonated_by": (imp.get("display_name") or imp.get("username")) if imp else None,
            "stop_url": myauth.stop_impersonating_url(here, here) if imp else None}
