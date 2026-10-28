"""Standalone mode: one person, their own machine, no MyAuth.

`garmin-mcp login` signs in to Garmin once (e-mail, password, MFA code) and
writes the tokens to a file only that user can read; `garmin-mcp stdio` then
serves the MCP tools over stdio to Claude Desktop or Claude Code on the same
machine. The password is never stored. The OAuth1 token lasts about a year;
the short-lived OAuth2 access token is renewed from it when it is about to
expire, without touching Garmin's sign-in again.

The token file is $GARMIN_MCP_TOKENS, else
$XDG_CONFIG_HOME/garmin-mcp/tokens.json (default ~/.config/garmin-mcp/),
mode 0600 in a 0700 directory. Signing in and renewing come from the MyCommons
building block `garmin_login` (garmin_mcp/garmin_login/ is a copy).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .garmin_login import signin
from .garmin_api.client import GarminError
from .session import GarminSession, NotConnected

NOT_SIGNED_IN = ("not signed in to Garmin yet - run `garmin-mcp login` in a terminal "
                 "on this machine first")


def token_path() -> Path:
    env = os.environ.get("GARMIN_MCP_TOKENS")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "garmin-mcp" / "tokens.json"


def load() -> dict | None:
    """{oauth1, oauth2, account}, None when nobody signed in yet."""
    path = token_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data.get("oauth1"), dict) or not isinstance(data.get("oauth2"), dict):
        raise ValueError(f"{path} has no oauth1/oauth2 tokens - run `garmin-mcp login` again")
    return data


def save(tokens: dict) -> Path:
    """Write atomically, readable by this user only."""
    path = token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"oauth1": tokens["oauth1"], "oauth2": tokens["oauth2"],
                   "account": tokens.get("account") or ""}, f, indent=2)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def forget() -> bool:
    """Delete the tokens; True if there were any."""
    path = token_path()
    if not path.exists():
        return False
    path.unlink()
    return True


class LocalSession(GarminSession):
    """The standalone mode's only user, with the tokens from the file."""

    def __init__(self) -> None:
        super().__init__(user_id=0)

    async def client(self):
        async with self._lock:
            tokens = load()
            if tokens is None:
                raise NotConnected(NOT_SIGNED_IN)
            if signin.needs_renewal(tokens["oauth2"]):
                try:
                    tokens["oauth2"] = await asyncio.to_thread(signin.renew, tokens["oauth1"])
                except signin.GarminError as exc:
                    raise GarminError(f"{exc} Run `garmin-mcp login` again.") from exc
                save(tokens)
            return self._use(tokens["oauth2"]["access_token"],
                             tokens["oauth2"]["expires_at"], tokens.get("account"))
