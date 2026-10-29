# How it works

For contributors, and for anyone who wants to know what the code is doing with
their tokens.

## Module map

```
garmin_mcp/
  garmin_api/   Garmin Connect HTTP client, ~90 functions (vendored building
                block; never signs in)
  fit/          FIT reading: parser.py (metrics), streams.py (time series),
                devfields.py (Connect-IQ fields), best_efforts.py (vendored
                building block)
  fitview.py    FIT results shrunk to something a model can hold
  project.py    Garmin JSON shrunk the same way
  tools.py      The 21 MCP tools; docstrings are what the model reads
  session.py    One Garmin client per user, fed with access tokens from MyAuth
  users.py      The local row of a MyAuth user
  access/       Sign-in, access checks and Garmin access tokens from MyAuth
                (vendored building block, never edit here)
  db.py         SQLite schema
  oauth.py      OAuth 2.1 authorization server: thin layer over mcp_oauth/ (vendored
                building block), keeps the consent and error page
  web.py        Account page, the msk-ui page shell, signed_in (on access.web;
                addresses come from mcp_oauth.addresses)
  static/       ui/ (msk-ui, vendored), app.css, logo
  server.py     HTTP entry point
  cli.py        garmin-mcp serve (HTTP, MyAuth) / login, status, logout, stdio (standalone)
  local.py      Standalone mode: token file, LocalSession
  garmin_login/ Garmin sign-in and renewal (vendored building block, never edit here)
```

## One mechanism for every user

In the hosted mode the server is multi-tenant, and the users are MyAuth's. (The
standalone mode, `garmin-mcp stdio`, has exactly one user and passes a
`LocalSession` to `build_server` instead; see local.py.) The whole separation
lives in one place:

```python
class _McpEndpoint:
    async def __call__(self, scope, receive, send):
        user_id = oauth.access_token_user(_scope_bearer(scope) or "")
        if user_id is None:
            await _send_unauthorized(scope, send)
            return
        info = await myauth.access_of(user_id)          # 503 if MyAuth is down
        if info is None or users.ensure_user(user_id, info["username"], ...):
            await _send_unauthorized(scope, send)
            return
        sessions.CURRENT_USER.set(user_id)
        await self._server.session_manager.handle_request(scope, receive, send)
```

The bearer token is resolved to a user, MyAuth confirms that user may still use
the app, the user goes into a `ContextVar`, and only then does the request
reach the MCP session manager.
Tools resolve their session per call through `current_session()`; binding a
session at registration time would quietly make the server single-tenant again,
which is why `tools.register` takes a function rather than an instance.

That the ContextVar survives into the tool coroutine was verified before the
design was built on it, and is covered by
`test_two_accounts_never_see_each_other`.

The FIT cache is namespaced per user (`cache/<user_id>/`).
Those files are somebody's training data; a flat cache would leak between
users.

## MyAuth

MyAuth is the author's self-hosted login service (not published; the
endpoints it has to answer are listed in [hosted.md](hosted.md)). This server
keeps no passwords, no web sessions and no Garmin credentials (up to 0.1 it
had its own accounts with invitations, up to 0.2 its own Garmin tokens; both
are gone). It asks MyAuth, over the internal Docker network, one of three
questions:

- **A browser page** - `GET /api/session?app=mcp-garmin` with the `sso_session`
  cookie. The account page and the OAuth consent use it; without a session the
  browser goes to MyAuth's sign-in on the same host (`MYAUTH_PUBLIC_PORT`) and comes back.
  Cookies do not separate ports, so the MyAuth cookie arrives here.
- **An MCP call** carries only this server's OAuth bearer. `GET
  /api/internal/access?uid=&app=mcp-garmin` says whether the user may still use
  the app; MyAuth answers it only inside the Docker network (a request through
  the reverse proxy carries `X-Forwarded-For` and is refused). The OAuth
  refresh grant asks the same before it hands out a new access token.
- **A Garmin call** needs the user's Garmin access token. `GET
  /api/internal/garmin/token?uid=&app=mcp-garmin`, authenticated with this
  app's key (`MYAUTH_APP_KEY`), returns it - MyAuth renews it itself and hands
  it out with at least ten minutes left. `session.client()` asks before every
  use; a disconnect in MyAuth therefore takes effect within the cache time.

Both answers also carry the user's time zone (`timezone`, set by each user
in MyAuth). It goes into `session.CURRENT_TZ`, and `session.today()` makes a
tool's default "today" the user's day rather than the server's - the
container runs in `TZ`, which is only the fallback.

All answers are cached 30 seconds. MyAuth down means fail closed: 503 on the
MCP endpoint and the pages, and a refresh that fails with 503 *before* spending
the refresh token, so the client can retry.

The same cookie reaching every app behind one MyAuth has a second consequence: SameSite
cannot stop a form on another app's page from posting here with it. Every POST
that acts for the signed-in user (delete data, the OAuth consent) is refused
when its `Origin` is not this server's.

## Where the Garmin login runs

In the hosted mode: in MyAuth, since 0.3 - the one place that signs in to
Garmin, so that this server and every other app reading Garmin share one
sign-in, and a Garmin change is fixed once. Garmin's SSO sits behind
Cloudflare, which since March 2026 answers fresh logins from datacenter
addresses with 429 or 403; MyAuth runs on a home connection, where it works.
OAuth1 is valid for about a year and stays in MyAuth; this server only ever
holds a short-lived OAuth2 access token, in memory. The Garmin client
(`garmin_api/`) cannot sign in at all; it only uses the token it is handed.

In the standalone mode, `garmin-mcp login` signs in on your own computer
(`garmin_login/`) and keeps both tokens in a file only you can read; see
[standalone.md](standalone.md).

## Responses are projected, always

Garmin answers with hundreds of fields per activity, and `extract_streams`
produces up to 1500 points per channel. Everything goes through `project.py` or
`fitview.py`: a whitelist per shape, rounded numbers, empty values dropped, and
streams resampled to `max_points` with min/max/avg per channel.

This is not cosmetic. Handing raw payloads to a model burns the context window
the conversation needs, and it is the most common weakness of comparable
servers. A new tool without a projection is a bug.

## Errors have to survive the SDK

If a tool raises anything unexpected, the MCP SDK reports `Error executing tool
<name>` and swallows the cause. The most common failure here - Garmin not
connected in MyAuth yet - would be unreadable. So every tool is wrapped in `_guard`, which
translates `NotConnected`, `GarminError` and `ValueError` into `ToolError`;
only those reach the model as text.

## Storage

SQLite through the standard library, one connection per operation, no ORM.
Starlette runs sync endpoints in a thread pool, so a shared connection would be
used across threads; opening per call sidesteps that and costs nothing at this
scale.

One trap, learned the hard way: `db.conn()` commits **after** the `yield`, so an
exception passing through skips the commit. The failed-login counter of the old
accounts was ineffective for exactly that reason. Write first, leave the block,
then raise.

A `users` row exists only to hang grants and cache on; its id **is** the
MyAuth uid. The username is stored next to it for one case: a MyAuth database
rebuilt from scratch hands the same uid to somebody else. When the name behind a
uid changes, `ensure_user` drops everything of the previous owner first. A
database from 0.1 (e-mail accounts, ids unrelated to MyAuth) is refused at
startup for the same reason.

No Garmin credential is stored here at all; MyAuth keeps them, encrypted.

## OAuth

`oauth.py` is a small, complete OAuth 2.1 authorization server: dynamic client
registration (RFC 7591), authorization code with PKCE S256, refresh grant,
authorization server metadata (RFC 8414) and protected resource metadata
(RFC 9728). The shared part is the vendored building block `mcp_oauth`
(`garmin_mcp/mcp_oauth/`, used by the author's other MCP servers as well);
`oauth.py` keeps only the consent and error page.

**The refresh token stays the same** for the life of a grant (since 0.5.1): a
refresh hands out a new access token and extends the refresh token by 30
days; earlier access tokens stay valid until they expire. Rotating refresh
tokens made a connector used from several places (several Claude sessions,
the phone) ask to sign in again.

It exists because neither claude.ai nor ChatGPT offers a field for a static
bearer header. A static token was supported while the server was single-user and
was removed with multi-tenancy: it cannot be attributed to an account, so it
would be a master key.

Identity at the consent screen is the MyAuth session. Access tokens carry the
MyAuth uid; whether that user may still use the app is asked on every MCP call
(see MyAuth above), which is why revoking the app in MyAuth cuts live clients
off within the cache time.

## Garmin quirks worth knowing

Undocumented API, so these were found by observation:

- **Challenges live on two endpoints.** `adHocChallenge/historical` returns only
  *finished* challenges; the one running this month is exclusively under
  `adHocChallenge/active`. Querying one of them makes the current month look
  like it does not exist.
- **A running challenge reports `playerCount: 0`** and an empty player list. The
  real table only comes from the per-uuid detail call.
- **Wellness endpoints want the profile display id**, the GUID-like
  `socialProfile.displayName`, not the human name.
- **Sleep is filed under the morning you woke up.**
- **Training readiness has an `inputContext`.** `AFTER_WAKEUP_RESET` is the
  morning value from Garmin's own Morning Report; anything else means the watch
  has not synced since waking.
- **Gear v2 uses different field names** than the older endpoint: `name`,
  `gearType`, `distanceUsedMeters`.
- **Garmin's numbers and the FIT file disagree**, especially on elevation
  (server-side correction) and pauses. Both views are exposed on purpose:
  `get_activity` versus `analyze_activity_fit`.

## Vendored code

`access/`, `mcp_oauth/`, `garmin_login/`, `garmin_api/` and `fit/` are
complete copies of building blocks from the author's private repository
**MyCommons**, where each block has its own tests and version (in its
`__init__.py`). A script there (`MyCommons/sync.sh <block> garmin_mcp`) copies
a block into every project using it, so a fix is made once. **Never edit
them here** - a change made only in this repo would be overwritten by the
next sync; changes go upstream. `tests/test_access_copy.py` compares the
copies with MyCommons when it is cloned next to this repo, and is skipped
otherwise (in CI, and for everyone else).

- `access/` - MyAuth client: sessions, access checks, Garmin access tokens;
  `access.web` is the browser sign-in core and the cross-site check that
  `web.signed_in` uses.
- `mcp_oauth/` - the shared OAuth 2.1 authorization server (see OAuth above)
  and its addresses (`mcp_oauth.addresses`: PUBLIC_URL, MCP_URL, UI_URL).
- `garmin_login/` - Garmin sign-in and token renewal (standalone mode).
- `garmin_api/` - the Garmin Connect client, shared with a sibling project
  (activities, search, challenges, records, scores, ranges, weather, time in
  zones).
- `fit/` - FIT reading (parser, time series, developer fields, best efforts
  within one file), shared with the same sibling project; this server reads
  metrics, time series and legs from it and does not show the best efforts.

`static/ui/` is likewise a copy of **msk-ui**, the design system of the
author's web apps; app styles go into `static/app.css`, never into the copy.

## Tests

105 tests, no network, no MyAuth: `conftest.py` puts a fake in place of
`access.myauth._fetch_session`, `_fetch_access` and `_fetch_garmin`. The Garmin API is stubbed at the client boundary, FIT
files are synthesised with `fit-tool`, and the HTTP surface is driven through
Starlette's `TestClient`, including a full OAuth handshake the way claude.ai
walks it.

The ones that matter most:

- `test_two_accounts_never_see_each_other` - the tenancy boundary, down to the
  Garmin token each call carries
- `test_revoking_the_app_in_myauth_cuts_the_connector_off` - revocation works
- `test_a_reused_uid_does_not_inherit_the_old_data` - a rebuilt MyAuth cannot leak
- `test_both_endpoints_are_asked` - the challenge bug cannot come back
- `test_consent_from_another_site_is_refused` - no consent CSRF from other apps

## MCP SDK

Built on `mcp` 2.x (`mcp.server.mcpserver.MCPServer`). The upper bound in
`pyproject.toml` is deliberate: 2.0 already removed `mcp.server.fastmcp` once,
and a server that rebuilds its image on every deploy would ride into the next
such change unannounced.
