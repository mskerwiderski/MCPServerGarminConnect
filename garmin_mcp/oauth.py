"""OAuth for Garmin MCP's remote MCP endpoint: the MyCommons building block
`mcp_oauth` (garmin_mcp/mcp_oauth/ is a copy - never edit it here; change it in
MyCommons, run its tests and sync it into Garmin MCP, Libre MCP and
MyDocContainer). This module re-exports its names, so server.py and the tests
stay as they were, and keeps what is Garmin MCP's own: the consent page (with
a warning when Garmin is not connected in MyAuth) and the error page.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

from . import web
from .access import myauth
from .mcp_oauth import oauth as _block
from .mcp_oauth.oauth import (  # noqa: F401 - re-exported for server.py and the tests
    ACCESS_TTL_MS,
    REFRESH_TTL_MS,
    CODE_TTL_MS,
    SCOPE,
    _CODES,
    _b64url,
    _new_token,
    _verify_pkce_s256,
    prune_expired,
    access_token_user,
    get_protected_resource_metadata,
    get_authorization_server_metadata,
    post_register,
    _AuthorizeRequest,
    _redirect_error,
    _get_client,
    _parse_authorize,
    post_authorize,
    _mint_pair,
    _renew,
    post_token,
)
from .web import esc, page


async def get_authorize(request: Request) -> Response:
    user, err = await web.signed_in(request)
    if err is not None:
        return err
    req, err = _parse_authorize(request, dict(request.query_params))
    if err is not None:
        return err
    assert req is not None
    try:
        connected = await myauth.garmin_token(int(user["uid"])) is not None
    except (myauth.Unavailable, myauth.GarminRefused):
        connected = True                  # not known right now - no warning then
    return page(request, "Authorize access", _consent_html(req, user, connected), user=user)


def _consent_html(req: _AuthorizeRequest, user: dict, connected: bool) -> str:
    name = user.get("display_name") or user.get("username") or ""
    warn = ("" if connected else
            '<p class="error">Your Garmin account is not connected yet, so the tools '
            'will have nothing to answer with. Connect it on your MyAuth account page '
            '(linked from <a href="/account">this server\'s account page</a>) - this '
            'authorization stays valid.</p>')

    def hidden(k: str, v: str | None) -> str:
        return "" if v is None else f'<input type="hidden" name="{k}" value="{esc(v)}">'

    return f"""<h1>Authorize access</h1>
<div class="card narrow">
<p class="muted">Signed in as {esc(name)}</p>
<h2>{esc(req.client_name)}</h2>
<p class="muted small">Redirect: <code>{esc(req.redirect_uri)}</code></p>
<p>This app is requesting <b>read-only</b> access to <b>your</b> Garmin Connect
data. It cannot see any other account on this server.</p>
{warn}
<form method="post" action="/oauth/authorize">
{hidden("response_type", "code")}{hidden("client_id", req.client_id)}
{hidden("redirect_uri", req.redirect_uri)}{hidden("code_challenge", req.code_challenge)}
{hidden("code_challenge_method", "S256")}{hidden("state", req.state)}
{hidden("scope", req.scope)}
<div class="actions"><button name="decision" value="allow">Allow</button>
<button name="decision" value="deny" class="btn-ghost">Deny</button></div></form>
</div>"""


def _error_html(request: Request, msg: str, status: int = 400) -> Response:
    return page(request, "Error", f"<h1>Error</h1><p>{esc(msg)}</p>", status=status)


_block.configure(error_page=_error_html)
