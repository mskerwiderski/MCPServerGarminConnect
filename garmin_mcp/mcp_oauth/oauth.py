"""OAuth 2.1 authorization server for a remote MCP endpoint, multi-tenant -
the MyCommons building block `mcp_oauth`.

Copied into its host package by MyCommons/sync.sh (`<package>/mcp_oauth/`) and
used by Garmin MCP, Libre MCP, MyDocContainer's MCP and MyFITContainer's MCP.
Never edit a copy.
DCR, PKCE S256, refresh grant and the two discovery documents:

  - clients and tokens live in SQLite,
  - every auth code and token carries the user_id (= MyAuth uid) it was
    issued for,
  - the consent screen identifies the user through their MyAuth session, and
    a refresh only succeeds while MyAuth still allows that user this app.

**The refresh token stays the same** for the life of a grant: a refresh hands
out a new access token and extends the refresh token by REFRESH_TTL_MS;
earlier access tokens stay valid until they expire. So every place that uses
the same connector (several Claude sessions, the phone) keeps working,
whichever token it holds - rotating refresh tokens made the others fail with
invalid_grant (decided 2026-10-28). Several rows may therefore
share one refresh token, one per access token.

What the host package provides (the contract):
  ..db           conn(), now_ms(); tables oauth_clients and oauth_tokens
  ..web          async signed_in(request) -> (user, error response)
  ..access       the MyCommons building block `access` (myauth.access_of)
and, via configure(error_page=...), a function (request, message, status) ->
Response for errors shown in the browser. The addresses (issuer, connector,
consent page) are the block's own (`mcp_oauth.addresses`, env PUBLIC_URL /
MCP_URL / UI_URL, or callables via configure(public_url=..., mcp_url=...,
ui_url=...)); since 1.1.0 the host no longer provides them. The consent page itself
(`get_authorize`) is the host's: it uses `_parse_authorize` and posts to
`post_authorize`.

Neither claude.ai nor ChatGPT offers a field for a static bearer header, so for
those clients this authorization server is not optional.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import urllib.parse
from base64 import urlsafe_b64encode
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from .. import web
from . import addresses
from ..access import myauth
from ..db import conn, now_ms

ACCESS_TTL_MS = 60 * 60 * 1000              # 1 h
REFRESH_TTL_MS = 30 * 24 * 60 * 60 * 1000   # 30 d
CODE_TTL_MS = 60 * 1000                     # 60 s
SCOPE = "mcp"

# Auth codes live 60 seconds and are consumed once - memory is the right place.
_CODES: dict[str, dict] = {}


def _plain_error(request: Request, message: str, status: int) -> Response:
    return PlainTextResponse(f"Error: {message}", status_code=status)


_error_page = _plain_error


_UNSET = addresses._UNSET     # "argument omitted", shared with addresses.configure


def configure(error_page=_UNSET, *, public_url=_UNSET, mcp_url=_UNSET, ui_url=_UNSET) -> None:
    """The host's page for errors shown in the browser:
    error_page(request, message, status) -> Response (None: plain text).
    public_url / mcp_url / ui_url: callables for hosts whose settings are not
    PUBLIC_URL / MCP_URL / UI_URL (see `addresses`). An omitted argument stays
    as it is."""
    global _error_page
    if error_page is not _UNSET:
        _error_page = error_page or _plain_error
    addresses.configure(public_url=public_url, mcp_url=mcp_url, ui_url=ui_url)


def _error_html(request: Request, message: str, status: int = 400) -> Response:
    return _error_page(request, message, status)


def _b64url(data: bytes) -> str:
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _new_token() -> str:
    return _b64url(secrets.token_bytes(32))


def _verify_pkce_s256(verifier: str, challenge: str) -> bool:
    computed = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return hmac.compare_digest(computed, challenge)


# --- storage ---------------------------------------------------------------


def prune_expired() -> None:
    now = now_ms()
    for code, rec in list(_CODES.items()):
        if rec["expires_at_ms"] < now:
            del _CODES[code]
    with conn() as c:
        c.execute("DELETE FROM oauth_tokens WHERE refresh_expires_at_ms < ?", (now,))


def access_token_user(token: str) -> int | None:
    """The user this bearer was issued to, or None. Whether MyAuth still
    allows that user is the MCP endpoint's second question."""
    if not token:
        return None
    with conn() as c:
        row = c.execute("SELECT user_id FROM oauth_tokens WHERE access_token=?"
                        " AND access_expires_at_ms >= ?", (token, now_ms())).fetchone()
    return int(row["user_id"]) if row else None


# --- discovery -------------------------------------------------------------


def get_protected_resource_metadata(request: Request) -> Response:
    return JSONResponse({
        "resource": addresses.mcp_url(request),
        "authorization_servers": [addresses.public_base(request)],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
    })


def get_authorization_server_metadata(request: Request) -> Response:
    base = addresses.public_base(request)
    return JSONResponse({
        "issuer": base,
        # a browser page: may live on a private host (UI_URL), not the public one
        "authorization_endpoint": f"{addresses.ui_base(request)}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
    })


# --- dynamic client registration -------------------------------------------


async def post_register(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:                                    # noqa: BLE001
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)

    uris_in = body.get("redirect_uris") if isinstance(body, dict) else None
    redirect_uris = [u for u in uris_in if isinstance(u, str)] if isinstance(uris_in, list) else []
    if not redirect_uris:
        return JSONResponse({"error": "invalid_redirect_uri",
                             "error_description": "redirect_uris required"}, status_code=400)
    for u in redirect_uris:
        parsed = urllib.parse.urlparse(u)
        if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
            return JSONResponse({"error": "invalid_redirect_uri",
                                 "error_description": f"must be https: {u}"}, status_code=400)

    name_in = body.get("client_name") if isinstance(body, dict) else None
    name = (name_in.strip()[:200] if isinstance(name_in, str) and name_in.strip()
            else "Unnamed MCP client")
    client_id = _new_token()
    with conn() as c:
        c.execute("INSERT INTO oauth_clients (client_id, client_name, redirect_uris,"
                  " created_at_ms) VALUES (?,?,?,?)",
                  (client_id, name, json.dumps(redirect_uris), now_ms()))
    return JSONResponse({
        "client_id": client_id,
        "client_name": name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "client_id_issued_at": now_ms() // 1000,
        "scope": SCOPE,
    }, status_code=201)


# --- authorize -------------------------------------------------------------


@dataclass(frozen=True)
class _AuthorizeRequest:
    client_id: str
    redirect_uri: str
    code_challenge: str
    state: str | None
    scope: str | None
    client_name: str


def _redirect_error(redirect_uri: str, state: str | None, error: str, desc: str) -> Response:
    url = urllib.parse.urlparse(redirect_uri)
    q = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
    q.update({"error": error, "error_description": desc})
    if state:
        q["state"] = state
    return RedirectResponse(url._replace(query=urllib.parse.urlencode(q)).geturl(),
                            status_code=302)


def _get_client(client_id: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM oauth_clients WHERE client_id=?",
                        (client_id,)).fetchone()
    return dict(row) if row else None


def _parse_authorize(request: Request, params: dict[str, str]
                     ) -> tuple[_AuthorizeRequest | None, Response | None]:
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    if not client_id:
        return None, _error_html(request, "Missing client_id", 400)
    client = _get_client(client_id)
    if not client:
        return None, _error_html(request, "Unknown client_id", 400)
    if not redirect_uri or redirect_uri not in json.loads(client["redirect_uris"]):
        return None, _error_html(request, "redirect_uri not registered for this client", 400)
    if params.get("response_type") != "code":
        return None, _redirect_error(redirect_uri, params.get("state"),
                                     "unsupported_response_type", "only code supported")
    code_challenge = params.get("code_challenge")
    if not code_challenge or (params.get("code_challenge_method") or "S256") != "S256":
        return None, _redirect_error(redirect_uri, params.get("state"),
                                     "invalid_request", "PKCE S256 required")
    return _AuthorizeRequest(client_id, redirect_uri, code_challenge,
                             params.get("state"), params.get("scope"),
                             client["client_name"]), None


async def post_authorize(request: Request) -> Response:
    user, err = await web.signed_in(request)
    if err is not None:
        return err
    user_id = int(user["uid"])
    form = await request.form()
    params = {k: str(v) for k, v in form.items() if isinstance(v, str)}
    req, err = _parse_authorize(request, params)
    if err is not None:
        return err
    assert req is not None
    if str(form.get("decision") or "") != "allow":
        return _redirect_error(req.redirect_uri, req.state, "access_denied", "denied")

    code = _new_token()
    _CODES[code] = {"client_id": req.client_id, "redirect_uri": req.redirect_uri,
                    "code_challenge": req.code_challenge, "scope": req.scope,
                    "user_id": user_id, "expires_at_ms": now_ms() + CODE_TTL_MS}
    url = urllib.parse.urlparse(req.redirect_uri)
    q = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
    q["code"] = code
    if req.state:
        q["state"] = req.state
    return RedirectResponse(url._replace(query=urllib.parse.urlencode(q)).geturl(),
                            status_code=302)


# --- token -----------------------------------------------------------------


def _mint_pair(client_id: str, user_id: int, scope: str | None) -> dict:
    access, refresh = _new_token(), _new_token()
    now = now_ms()
    with conn() as c:
        c.execute("INSERT INTO oauth_tokens (access_token, refresh_token, client_id,"
                  " user_id, scope, access_expires_at_ms, refresh_expires_at_ms)"
                  " VALUES (?,?,?,?,?,?,?)",
                  (access, refresh, client_id, user_id, scope,
                   now + ACCESS_TTL_MS, now + REFRESH_TTL_MS))
    return {"access_token": access, "token_type": "Bearer",
            "expires_in": ACCESS_TTL_MS // 1000, "refresh_token": refresh,
            "scope": scope or SCOPE}


def _renew(row) -> dict:
    """A new access token for the grant of `row`, the same refresh token with a
    fresh lifetime. Earlier access tokens of the grant stay valid until they
    expire; expired ones are dropped."""
    access, now = _new_token(), now_ms()
    with conn() as c:
        c.execute("DELETE FROM oauth_tokens WHERE refresh_token=? AND access_expires_at_ms < ?",
                  (row["refresh_token"], now))
        c.execute("UPDATE oauth_tokens SET refresh_expires_at_ms=? WHERE refresh_token=?",
                  (now + REFRESH_TTL_MS, row["refresh_token"]))
        c.execute("INSERT INTO oauth_tokens (access_token, refresh_token, client_id,"
                  " user_id, scope, access_expires_at_ms, refresh_expires_at_ms)"
                  " VALUES (?,?,?,?,?,?,?)",
                  (access, row["refresh_token"], row["client_id"], row["user_id"],
                   row["scope"], now + ACCESS_TTL_MS, now + REFRESH_TTL_MS))
    return {"access_token": access, "token_type": "Bearer",
            "expires_in": ACCESS_TTL_MS // 1000, "refresh_token": row["refresh_token"],
            "scope": row["scope"] or SCOPE}


async def post_token(request: Request) -> Response:
    prune_expired()
    try:
        form = await request.form()
    except Exception:                                    # noqa: BLE001
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    def g(k: str) -> str | None:
        v = form.get(k)
        return v if isinstance(v, str) else None

    grant = g("grant_type")
    if grant == "authorization_code":
        code, redirect_uri = g("code"), g("redirect_uri")
        client_id, verifier = g("client_id"), g("code_verifier")
        if not all([code, redirect_uri, client_id, verifier]):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        rec = _CODES.pop(code, None)
        if (not rec or rec["expires_at_ms"] < now_ms()
                or rec["client_id"] != client_id or rec["redirect_uri"] != redirect_uri
                or not _verify_pkce_s256(verifier, rec["code_challenge"])):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse(_mint_pair(rec["client_id"], rec["user_id"], rec["scope"]))

    if grant == "refresh_token":
        refresh_token, client_id = g("refresh_token"), g("client_id")
        if not refresh_token or not client_id:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        with conn() as c:
            row = c.execute("SELECT * FROM oauth_tokens WHERE refresh_token=?"
                            " ORDER BY access_expires_at_ms DESC", (refresh_token,)).fetchone()
        if (row is None or row["client_id"] != client_id
                or row["refresh_expires_at_ms"] < now_ms()):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        try:
            allowed = await myauth.access_of(int(row["user_id"])) is not None
        except myauth.Unavailable:
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
        if not allowed:                     # blocked in MyAuth or the app revoked
            with conn() as c:
                c.execute("DELETE FROM oauth_tokens WHERE refresh_token=?", (refresh_token,))
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse(_renew(row))

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# --- html ------------------------------------------------------------------


