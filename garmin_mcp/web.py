"""The small web surface: the account page and the page shell every HTML
response shares (msk-ui, the author's design system). Plain HTML
strings, no template engine - this is two pages.

Nobody signs in here, not even to Garmin. `signed_in` asks MyAuth who the
browser belongs to; the sign-in page, the user's account, the log-out and the
Garmin connection are MyAuth's.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import __version__, users
from .access import myauth
from .access import web as access_web
from .mcp_oauth import addresses

STATIC_DIR = Path(__file__).parent / "static"
_asset_cache: dict[str, str] = {}

LOGO = """<svg class="brand-logo" viewBox="0 0 64 64" aria-hidden="true">
<defs><linearGradient id="hdrGrad" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#4f8cff"/><stop offset="1" stop-color="#7b6cff"/></linearGradient></defs>
<rect x="4.5" y="4.5" width="55" height="55" rx="15" fill="#0e141d" stroke="url(#hdrGrad)" stroke-width="3"/>
<rect x="26" y="11.5" width="12" height="8" rx="3" fill="#4f8cff"/>
<rect x="26" y="44.5" width="12" height="8" rx="3" fill="#4f8cff"/>
<circle cx="32" cy="32" r="12.5" fill="#0e141d" stroke="#0bb4a6" stroke-width="4.5"/>
<polyline points="24.5,33 28.5,33 30.5,27.5 33.5,37.5 35.5,33 39.5,33" fill="none"
 stroke="#7b6cff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>"""

_ICON_WATCH = ('<svg viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor"'
               ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
               '<circle cx="12" cy="12" r="5.5"/><path d="M9.5 6.5 10 3h4l.5 3.5M9.5 17.5 10 21h4'
               'l.5-3.5M9.8 12.2h1.2l.7-1.6 1 3 .6-1.4h1"/></g></svg>')
_ICON_USER = ('<svg viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor"'
              ' stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="8.5" r="3.8"/>'
              '<path d="M4.5 20a7.5 7.5 0 0 1 15 0"/></g></svg>')
_ICON_LOGOUT = ('<svg viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor"'
                ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
                '<path d="M9.5 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h3.5"/>'
                '<path d="M15 8l4 4-4 4M19 12H9.5"/></g></svg>')


def esc(s: object) -> str:
    return html_lib.escape(str(s), quote=True)


def asset(rel: str) -> str:
    """Static path with a content hash as ?v= (cache busting)."""
    if rel not in _asset_cache:
        try:
            v = hashlib.md5((STATIC_DIR / rel).read_bytes()).hexdigest()[:8]
        except OSError:
            v = "0"
        _asset_cache[rel] = f"/static/{rel}?v={v}"
    return _asset_cache[rel]


# --- addresses ---------------------------------------------------------------
#
# The server's addresses (issuer, connector, consent page) are the MyCommons
# block's: mcp_oauth.addresses (env PUBLIC_URL / MCP_URL / UI_URL, docs/hosted.md).
# The sign-in core and the cross-site check are access.web's.


def page(request: Request, title: str, body: str, status: int = 200,
         user: dict | None = None) -> HTMLResponse:
    nav = tabbar = banner = ""
    if user:
        acc = access_web.account(request, user)
        account = esc(acc["account_url"])
        logout = esc(acc["logout_url"])
        name = esc(acc["name"] or "")
        if acc["impersonated_by"]:
            # an admin signed in as this user ("sign in as" in MyAuth) - a consent
            # given now would be the user's, so it shows on every page
            admin = esc(acc["impersonated_by"])
            stop = esc(acc["stop_url"])
            banner = (f'<div class="imp-banner" role="status"><span>You are signed in as '
                      f'{name} - on behalf of {admin}.</span>'
                      f'<a class="btn btn-sm" href="{stop}">Sign out as {name}</a></div>\n')
        nav = (f'<span class="nav-user">{name} <a href="{account}">Account</a>'
               f' <a href="{logout}">Log out</a></span>')
        here = "active" if request.url.path.startswith("/account") else ""
        tabbar = (f'<nav class="tabbar-bottom" aria-label="Main navigation">'
                  f'<a class="tab {here}" href="/account">{_ICON_WATCH}<span>Garmin MCP</span></a>'
                  f'<a class="tab" href="{account}">{_ICON_USER}<span>Account</span></a>'
                  f'<a class="tab" href="{logout}">{_ICON_LOGOUT}<span>Log out</span></a></nav>')
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<meta name="theme-color" content="#0a0e13" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#f3f5f9" media="(prefers-color-scheme: light)">
<title>Garmin MCP · {esc(title)}</title>
<link rel="icon" type="image/svg+xml" href="{asset('favicon.svg')}">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Garmin MCP">
<link rel="apple-touch-icon" href="{asset('apple-touch-icon.png')}">
<link rel="manifest" href="{asset('site.webmanifest')}">
<link rel="stylesheet" href="{asset('ui/tokens.css')}">
<link rel="stylesheet" href="{asset('ui/components.css')}">
<link rel="stylesheet" href="{asset('app.css')}">
</head>
<body>
{banner}<header><a href="/account" class="brand">{LOGO}Garmin MCP</a><nav>{nav}</nav></header>
<main>{body}</main>
{tabbar}
<footer>Garmin MCP v{__version__}{_footer_note()}</footer>
</body>
</html>""", status_code=status)


def _footer_note() -> str:
    """FOOTER_NOTE (e.g. "Private service · Owner: ...") follows the version."""
    note = os.environ.get("FOOTER_NOTE", "").strip()
    return f" · {esc(note)}" if note else ""


# --- who is asking -----------------------------------------------------------


async def signed_in(request: Request) -> tuple[dict | None, Response | None]:
    """The MyAuth user behind this browser, with their local row brought up to
    date - or the response to send instead: MyAuth's sign-in page, "no access",
    or "sign-in unavailable". Apps behind one MyAuth share a host name and
    cookies do not separate ports, so a POST from another site is refused
    (access.web.cross_site)."""
    if request.method == "POST" and access_web.cross_site(request):
        return None, page(request, "Refused", "<h1>Refused</h1><p>This form was sent "
                          "from another site.</p>", 403)
    gate = await access_web.check(request)
    if gate.outcome == access_web.OK and "uid" in gate.user:
        body = gate.user
        users.ensure_user(int(body["uid"]), str(body.get("username") or ""),
                          str(body.get("display_name") or ""))
        return body, None
    if gate.outcome == access_web.UNAVAILABLE:
        return None, page(request, "Sign-in unavailable",
                          "<h1>Sign-in unavailable</h1><p>MyAuth, the sign-in service, "
                          "is not answering. Please try again in a moment.</p>", 503)
    if gate.outcome == access_web.NO_ACCESS:
        account = esc(access_web.myauth_page(request, "/"))
        return None, page(request, "No access",
                          "<h1>No access</h1><p>Your MyAuth account is not enabled for "
                          "Garmin MCP. An administrator can change that in "
                          f'<a href="{account}">MyAuth</a>.</p>', 403)
    target = access_web.login_target(
        request, password_change=gate.outcome == access_web.CHANGE_PASSWORD)
    if request.method in ("GET", "HEAD"):
        return None, RedirectResponse(target, status_code=303)
    return None, page(request, "Signed out", "<h1>Signed out</h1><p>Your MyAuth session "
                      f'has ended. <a href="{esc(target)}">Sign in again</a>.</p>', 401)


# --- account -----------------------------------------------------------------


_COPY_JS = """<script>
function copyText(id, btn) {
  const field = document.getElementById(id);
  navigator.clipboard.writeText(field.value).then(function () {
    const before = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(function () { btn.textContent = before; }, 1500);
  }, function () { field.select(); });
}
</script>"""


async def get_account(request: Request) -> Response:
    user, err = await signed_in(request)
    if err is not None:
        return err
    myauth_page = esc(access_web.myauth_page(request, "/connections#garmin"))
    try:
        token = await myauth.garmin_token(int(user["uid"]))
    except (myauth.Unavailable, myauth.GarminRefused) as exc:
        token, problem = None, str(exc)
    else:
        problem = ""
    if token:
        garmin = (f'<div class="status-line"><span class="badge ok">connected</span>'
                  f' as <b>{esc(token.get("account") or "Garmin")}</b></div>'
                  f'<p class="muted small">The connection lives in MyAuth; manage or '
                  f'disconnect it on your <a href="{myauth_page}">MyAuth account page</a>.</p>')
    elif problem:
        garmin = (f'<div class="status-line"><span class="badge warn">not available</span></div>'
                  f'<p class="error">{esc(problem)}</p>'
                  f'<a class="btn" href="{myauth_page}">Open MyAuth</a>')
    else:
        garmin = ('<div class="status-line"><span class="badge warn">not connected</span></div>'
                  '<p class="muted">The tools cannot answer anything until you connect '
                  'your Garmin account. That happens once, in MyAuth - every app that '
                  f'reads Garmin uses the same connection.</p>'
                  f'<a class="btn" href="{myauth_page}">Connect Garmin in MyAuth</a>')
    url = addresses.mcp_url(request)
    return page(request, "Account", f"""<h1>Garmin MCP</h1>
<div class="card"><h2 class="first">Garmin Connect</h2>{garmin}</div>
<div class="card"><h2 class="first">Use it in Claude, ChatGPT or Mistral</h2>
<p>Add a custom connector with this address - the same one for every client,
exactly as shown:</p>
<textarea id="mcp_url" class="connector-url" rows="1" readonly>{esc(url)}</textarea>
<div class="actions"><button type="button" onclick="copyText('mcp_url', this)">Copy address</button></div>
<p class="muted small">Adding it asks you once to confirm access, signed in with MyAuth. Do
that on a device that can reach MyAuth (it may be private). Afterwards the
connector works from anywhere. There is nothing else to enter: no API key, no token.</p>
</div>{_COPY_JS}
<div class="card"><h2 class="first">Your data on this server</h2>
<p class="muted">The connectors you allowed and the cached FIT files. Deleting them
disconnects every connector; your MyAuth account and its Garmin connection stay as
they are.</p>
<form method="post" action="/account/delete"
onsubmit="return confirm('Delete your connector authorizations and cached files?')">
<button class="danger">Delete my data</button></form></div>""", user=user)


async def post_delete_account(request: Request) -> Response:
    user, err = await signed_in(request)
    if err is not None:
        return err
    from .session import forget_user
    forget_user(int(user["uid"]))
    users.delete_user(int(user["uid"]))
    return RedirectResponse("/account", status_code=303)
