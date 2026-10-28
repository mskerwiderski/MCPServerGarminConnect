"""The HTTP server: streamable MCP, OAuth, and the account page.

Multi-tenant. It resolves the bearer token to a user,
asks MyAuth whether that user may still use this app, puts the user into a
ContextVar and only then hands the request to the MCP session manager; the
tools read the ContextVar. That is the whole tenancy mechanism, and it is why
no request can reach another user's data.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from . import db, oauth, session as sessions, tools, users, web
from .access import myauth

# httpx logs every request URL at INFO (the MCP SDK switches logging on).
# Internal MyAuth calls and Garmin URLs have no business in the log.
logging.getLogger("httpx").setLevel(logging.WARNING)

INSTRUCTIONS = """Read-only access to one Garmin Connect account.

Activities: list_activities, get_activity (Garmin's own numbers),
analyze_activity_fit / get_activity_streams / get_swim_detail /
get_activity_sensors (the original file the watch wrote).
Health: get_daily_health, get_sleep_detail (phases and the night's series:
movement, heart rate, HRV, stress, Body Battery, SpO2, respiration),
get_training_status, get_body_composition, get_blood_pressure.
Challenges: list_challenges, get_challenge.
Context: get_profile, list_gear, whoami.

Nothing in this server writes to Garmin Connect."""


def build_server(get_session=None) -> MCPServer:
    server = MCPServer(name="garmin-connect", instructions=INSTRUCTIONS)
    tools.register(server, get_session or sessions.current_session)
    return server


# --- HTTP ------------------------------------------------------------------


def _scope_bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            val = value.decode("latin-1")
            if val.startswith("Bearer "):
                return val[7:].strip()
    return None


async def _send_json(send: Send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]})
    await send({"type": "http.response.body", "body": body})


async def _send_unauthorized(scope: Scope, send: Send) -> None:
    body = json.dumps({"error": "unauthorized"}).encode()
    meta = web.well_known(web.mcp_url(Request(scope)), "oauth-protected-resource")
    www_auth = f'Bearer realm="mcp", resource_metadata="{meta}"'
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"www-authenticate", www_auth.encode("latin-1")),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]})
    await send({"type": "http.response.body", "body": body})


class _McpEndpoint:
    def __init__(self, server: MCPServer) -> None:
        self._server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        user_id = oauth.access_token_user(_scope_bearer(scope) or "")
        if user_id is None:
            await _send_unauthorized(scope, send)
            return
        # Blocked in MyAuth, app revoked, or the uid now belongs to somebody
        # else: the bearer is worthless from this request on.
        try:
            info = await myauth.access_of(user_id)
        except myauth.Unavailable:
            await _send_json(send, 503, {"error": "sign-in service unavailable"})
            return
        if info is None or users.ensure_user(user_id, str(info.get("username") or ""),
                                             str(info.get("display_name") or "")):
            await _send_unauthorized(scope, send)
            return
        sessions.CURRENT_USER.set(user_id)
        sessions.CURRENT_TZ.set(info.get("timezone"))
        await self._server.session_manager.handle_request(scope, receive, send)


def build_http_app() -> Starlette:
    myauth.require_configured()         # fail fast: no MyAuth, no users and no Garmin
    db.init()
    server = build_server()
    # Initialises the session manager lazily; the returned app is not mounted,
    # the endpoint below drives the same manager with our own auth in front.
    server.streamable_http_app(
        json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def root(_request: Request) -> RedirectResponse:
        return RedirectResponse("/account", status_code=302)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        oauth.prune_expired()
        async with server.session_manager.run():
            yield
        await sessions.close_all()

    return Starlette(
        middleware=[Middleware(
            CORSMiddleware, allow_origins=["*"],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["*"],
            expose_headers=["WWW-Authenticate"], allow_credentials=False)],
        routes=[
            Route("/", root),
            Route("/health", health),
            Route("/healthz", health),
            Mount("/static", StaticFiles(directory=web.STATIC_DIR), name="static"),
            # web - signing in and out is MyAuth's
            Route("/account", web.get_account),
            Route("/account/delete", web.post_delete_account, methods=["POST"]),
            # OAuth discovery (RFC 8414 + RFC 9728)
            Route("/.well-known/oauth-protected-resource",
                  oauth.get_protected_resource_metadata),
            Route("/.well-known/oauth-protected-resource/mcp",
                  oauth.get_protected_resource_metadata),
            Route("/.well-known/oauth-authorization-server",
                  oauth.get_authorization_server_metadata),
            Route("/oauth/register", oauth.post_register, methods=["POST"]),
            Route("/oauth/authorize", oauth.get_authorize),
            Route("/oauth/authorize", oauth.post_authorize, methods=["POST"]),
            Route("/oauth/token", oauth.post_token, methods=["POST"]),
            Route("/mcp", _McpEndpoint(server), methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )


def __getattr__(name: str):
    """PEP 562: `uvicorn garmin_mcp.server:app` builds the app on attribute
    access, so merely importing this module has no side effects."""
    if name == "app":
        return build_http_app()
    raise AttributeError(name)
