# MCPServerGarminConnect

Read-only MCP server for Garmin Connect (package `garmin_mcp`, command
`garmin-mcp`): standalone for one person (`garmin-mcp stdio`) or hosted and
multi-tenant behind MyAuth (`garmin-mcp serve`). Public repository.

**Read [docs/architecture.md](docs/architecture.md) before changing anything.**
It carries the design decisions and the Garmin quirks behind them; this file
only adds what is specific to working in this repository.

Documentation, code comments and the web UI are English.

The author's own hosted instance is operated from a private infrastructure
repo; nothing about it (host names, addresses, keys, deploy commands) belongs
here. Examples use placeholders such as `https://mcp.example.com/garmin`.

## Rules that are easy to break

- **Vendored building blocks are never edited here.** `garmin_mcp/access/`,
  `mcp_oauth/`, `garmin_login/`, `garmin_api/` and `fit/` are copies from the
  author's private repo MyCommons: change the block there, run its tests, bump
  its version, then `MyCommons/sync.sh <block> garmin_mcp` (and into its
  other users). `tests/test_access_copy.py` fails locally when a copy is out
  of date (skipped when MyCommons is not cloned next to this repo).
  `__init__.py` calls `myauth.configure(app="mcp-garmin", platform="Garmin")`.
- **`garmin_api/` never signs in.** Signing in to Garmin is MyAuth's job in the
  hosted mode and `garmin_login/` in the standalone mode; this server hands
  the client an access token (`session.py`, `local.py`).
- **Standalone mode** (`local.py`, `garmin-mcp login/status/logout/stdio`):
  one user, tokens in `~/.config/garmin-mcp/tokens.json` (0600). It is the
  only place this server signs in to Garmin itself. stdio means stdout belongs
  to the MCP protocol - never print there in that path. Guide:
  docs/standalone.md.
- **Every tool needs a projection.** Raw Garmin JSON in a response is a bug -
  see the projection section in the architecture document.
- **Every tool needs `_guard`.** Without it the SDK swallows the cause and the
  model sees `Error executing tool <name>`.
- **Never bind a session at registration time.** `tools.register` takes a
  resolver function; an instance would make the server single-tenant.
- **`db.conn()` skips its commit when an exception passes through.** Commit
  before raising.
- **Docstrings are user interface.** They are what the model reads when deciding
  which tool to call.
- **Users are MyAuth's.** `users.id` is the MyAuth uid; nothing here creates,
  blocks or administers users. `access.myauth._fetch_session`,
  `_fetch_access` and `_fetch_garmin` are the mock points - no
  test talks to MyAuth.
- **Every POST acting for the signed-in user goes through `web.signed_in`**,
  which also refuses foreign `Origin`s: apps behind one MyAuth share one
  cookie host.
- **"Sign in as" shows on every page.** `/api/session` carries
  `impersonated_by` while an admin acts as the user; `web.page` then puts a
  banner with MyAuth's `/impersonate/stop?next=...` above the page - on the
  consent page too, since a connector allowed then is the user's.
- **Look & feel is msk-ui.** The copy in `garmin_mcp/static/ui/` comes from
  the author's design system and is never edited here; app styles go into
  `static/app.css`, tokens only.
- **No host-specific values in the repo.** Addresses and keys of a hosted
  instance are environment variables (`.env.example`, docs/hosted.md).

## Working on it

```bash
uv sync --extra dev
uv run pytest
uvx ruff check --select E9,F garmin_mcp tests
```

Tests never touch the network or the real `~/.garmin-mcp`; `conftest.py` points
every path at a temporary directory and fakes MyAuth.

## Hosted mode

`docker-compose.yml` builds the image; configuration comes from `.env`
(`.env.example`) plus a local `docker-compose.override.yml` for the network
shared with MyAuth. Requirements, the MyAuth endpoints, reverse proxy and
troubleshooting: [docs/hosted.md](docs/hosted.md).
