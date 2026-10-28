"""One Garmin client per user, plus the registry that hands them out.

The user is the one behind the bearer token of the MCP request (a MyAuth
uid). Their Garmin access comes from MyAuth, which is the only place in a
hosted setup that signs in to Garmin and renews tokens: every `client()` call asks
MyAuth for the current access token (cached 30 s in `myauth`), so a
disconnect or a reconnect there takes effect here right away. Nothing about
Garmin credentials is stored on this server.

The FIT cache is namespaced per user: those files are somebody's training
data, and two accounts must never read each other's.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import shutil
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .access import myauth
from .garmin_api.client import GarminClient, GarminError, OAuth2Token

CACHE_MAX_FILES = 40

# Set by the HTTP endpoint before it hands the request to the MCP session
# manager. Verified to survive into the tool coroutine.
CURRENT_USER: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "garmin_mcp_user", default=None)
# The user's time zone as set in MyAuth (`timezone`).
CURRENT_TZ: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "garmin_mcp_tz", default=None)


def today() -> date:
    """Today where the user is - "today" in a tool means their day, not the
    server's (the container runs in UTC or TZ)."""
    try:
        zone = ZoneInfo(CURRENT_TZ.get()) if CURRENT_TZ.get() else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    return datetime.now(zone).date() if zone else date.today()


class NotConnected(RuntimeError):
    """The user has not connected Garmin in MyAuth."""


def cache_root() -> Path:
    env = os.environ.get("GARMIN_MCP_CACHE")
    return Path(env).expanduser() if env else Path.home() / ".garmin-mcp" / "cache"


class GarminSession:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self._client: GarminClient | None = None
        self._lock = asyncio.Lock()
        self.account = ""

    async def client(self) -> GarminClient:
        try:
            token = await myauth.garmin_token(self.user_id)
        except myauth.Unavailable as exc:
            raise GarminError("MyAuth, which holds the Garmin connection, is not "
                              "answering. Try again in a moment.") from exc
        except myauth.GarminRefused as exc:
            raise GarminError(f"Garmin refused to renew the access: {exc}. Connect "
                              "Garmin again on your MyAuth account page.") from exc
        if token is None:
            raise NotConnected("your Garmin account is not connected yet - open your "
                               "MyAuth account page and connect Garmin there")
        async with self._lock:
            return self._use(token["access_token"], token["expires_at"], token.get("account"))

    def _use(self, access_token: str, expires_at: float, account: str | None) -> GarminClient:
        """Hand the access token to this user's client. Only the access token:
        renewing it is the caller's job (MyAuth, or the standalone mode in
        local.py), so the client has no OAuth1 token and never tries to
        refresh itself."""
        if self._client is None:
            self._client = GarminClient()
        self._client.oauth2_token = OAuth2Token(
            access_token=access_token, refresh_token="", expires_in=0,
            expires_at=float(expires_at))
        self.account = str(account or "")
        return self._client

    async def display_id(self) -> str:
        c = await self.client()
        return await c.profile_display_id()

    @property
    def cache_dir(self) -> Path:
        return cache_root() / str(self.user_id)

    async def fit_bytes(self, activity_id: int) -> bytes | None:
        path = self.cache_dir / f"{activity_id}.fit"
        if path.exists():
            os.utime(path, None)
            return path.read_bytes()
        c = await self.client()
        data = await c.download_original_fit(activity_id)
        if data:
            self._store_cached(path, data)
        return data

    def _store_cached(self, path: Path, data: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            files = sorted(path.parent.glob("*.fit"), key=lambda p: p.stat().st_mtime)
            for old in files[:-CACHE_MAX_FILES]:
                old.unlink(missing_ok=True)
        except OSError:                  # read-only filesystem - the cache is optional
            pass

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# --- registry --------------------------------------------------------------

_SESSIONS: dict[int, GarminSession] = {}


def session_for_user(user_id: int) -> GarminSession:
    if user_id not in _SESSIONS:
        _SESSIONS[user_id] = GarminSession(user_id)
    return _SESSIONS[user_id]


def current_session() -> GarminSession:
    """Whose data this request is about."""
    user_id = CURRENT_USER.get()
    if user_id is None:
        raise RuntimeError("no user bound to this request")
    return session_for_user(user_id)


def forget_user(user_id: int) -> None:
    """Drop the cached client and every cached FIT of that user. Called when
    they delete their data here."""
    session = _SESSIONS.pop(user_id, None)
    if session is not None and session._client is not None:
        client = session._client
        session._client = None
        try:
            asyncio.get_running_loop().create_task(client.aclose())
        except RuntimeError:                       # no loop - let GC handle it
            pass
    shutil.rmtree(cache_root() / str(user_id), ignore_errors=True)


async def close_all() -> None:
    for session in list(_SESSIONS.values()):
        await session.aclose()
    _SESSIONS.clear()


async def probe(session: GarminSession) -> dict:
    """Who this user is connected as - the `whoami` tool."""
    c = await session.client()
    name = await c.fetch_display_name()
    exp = c.oauth2_token.expires_at if c.oauth2_token else None
    return {"account": name or session.account, "oauth2_expires_at": exp}
