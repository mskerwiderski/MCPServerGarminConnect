"""garmin-mcp: the hosted HTTP server, or standalone on your own machine.

    garmin-mcp serve [--host 0.0.0.0] [--port 8000]   HTTP server, sign-in through MyAuth
    garmin-mcp login                                   standalone: sign in to Garmin once
    garmin-mcp status                                  standalone: who, and until when
    garmin-mcp logout                                  standalone: delete the tokens
    garmin-mcp stdio                                   standalone: MCP over stdio for
                                                       Claude Desktop / Claude Code

`serve` needs MyAuth (MYAUTH_URL, MYAUTH_APP_KEY): it decides who may use the
server and holds the Garmin connections. The standalone commands need nothing
but this machine; see local.py.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime

MFA_TRIES = 3


def _login() -> int:
    from . import local
    from .garmin_login import signin

    email = input("Garmin e-mail: ").strip()
    password = getpass.getpass("Garmin password: ")
    try:
        with signin.SignIn() as s:
            tokens = s.start(email, password)
            tries = 0
            while tokens is None:
                code = input("Code Garmin just sent you (e-mail or app): ")
                try:
                    tokens = s.submit_mfa(code)
                except signin.GarminError as exc:
                    tries += 1
                    if not s.waiting_for_mfa or tries >= MFA_TRIES:
                        raise
                    print(exc, file=sys.stderr)
    except signin.GarminError as exc:
        print(f"Sign-in failed: {exc}", file=sys.stderr)
        return 1
    path = local.save(tokens)
    who = tokens.get("account") or "your Garmin account"
    print(f"Signed in as {who}. Tokens saved to {path} (readable only by you).")
    print("Add `garmin-mcp stdio` to Claude Desktop or Claude Code; see the README.")
    return 0


def _status() -> int:
    from . import local

    try:
        tokens = local.load()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if tokens is None:
        print(f"Not signed in (no tokens at {local.token_path()}). Run `garmin-mcp login`.")
        return 1
    until = datetime.fromtimestamp(float(tokens["oauth2"]["expires_at"]))
    print(f"Signed in as {tokens.get('account') or '(unknown name)'}")
    print(f"Tokens: {local.token_path()}")
    print(f"Access token valid until {until:%Y-%m-%d %H:%M}; it is renewed automatically.")
    return 0


def _logout() -> int:
    from . import local

    if local.forget():
        print(f"Deleted {local.token_path()}.")
    else:
        print("There were no tokens to delete.")
    return 0


def _stdio() -> int:
    from .local import LocalSession
    from .server import build_server

    session = LocalSession()
    build_server(lambda: session).run(transport="stdio")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="garmin-mcp", description="Read-only MCP server for Garmin Connect")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the HTTP server (streamable HTTP on /mcp), "
                                         "sign-in through MyAuth")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("login", help="standalone: sign in to Garmin and store the tokens")
    sub.add_parser("status", help="standalone: show who is signed in")
    sub.add_parser("logout", help="standalone: delete the stored tokens")
    sub.add_parser("stdio", help="standalone: serve MCP over stdio (Claude Desktop/Code)")
    args = parser.parse_args(argv)

    if args.cmd == "login":
        return _login()
    if args.cmd == "status":
        return _status()
    if args.cmd == "logout":
        return _logout()
    if args.cmd == "stdio":
        return _stdio()

    import uvicorn

    from .server import build_http_app
    uvicorn.run(build_http_app(), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
