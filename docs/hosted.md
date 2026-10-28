# Hosted: running the server

`garmin-mcp serve` is the multi-user mode: one server, many users, each with
their own connector in claude.ai, ChatGPT or Mistral Vibe. For one person on
their own computer, the [standalone mode](standalone.md) is simpler and needs
none of this.

## What it needs: MyAuth

The hosted server keeps no accounts, no passwords and no Garmin credentials.
It signs users in through **MyAuth**, the author's self-hosted login service,
which also holds each user's Garmin connection. **MyAuth is not published**;
this guide describes how the author's own instance is set up, for
transparency. To run the hosted mode elsewhere you would need a service that
answers the same endpoints:

| Endpoint | Asked by | Answer |
|---|---|---|
| `GET /api/session?app=<key>` with the `sso_session` cookie | account and consent pages | 200 with the user (`uid`, `username`, `display_name`, `timezone`, optional `impersonated_by`), 401 not signed in or password change pending, 403 not allowed |
| `GET /api/internal/access?uid=&app=<key>` | every MCP call, every refresh | 200 with the user while they may use the app, else 403 |
| `GET /api/internal/garmin/token?uid=&app=<key>`, bearer = app key | every Garmin call | the user's current Garmin OAuth2 access token |
| `/login`, `/logout`, `/password`, `/connections`, `/impersonate/stop` (browser pages, each with `?next=`) | links from this server's pages | sign-in, sign-out, password change, connecting Garmin, ending "sign in as" |

The two internal endpoints must only be reachable from the server's own
network, never through the reverse proxy. The code that asks them is in
`garmin_mcp/access/myauth.py`.

In MyAuth the app is registered with a key (`mcp-garmin` by default), the
port of its pages and `"garmin": true`, and it gets an app key: the same
random value stands in MyAuth's `APP_KEYS` (`{"mcp-garmin": "..."}`) and in
this server's `MYAUTH_APP_KEY`. Users need the app enabled in MyAuth.

## Two faces

The server can be reached under two addresses, which may be the same:

| Address (example) | Who | Reachable |
|---|---|---|
| `https://mcp.example.com/garmin` | AI clients (claude.ai, ChatGPT, Mistral) | public - the AI providers call it from their data centres |
| `https://home.example.com:1030` | your browser: account page, consent page | may stay private (LAN or VPN), next to MyAuth |

Only the endpoints the AI clients call have to be public: the MCP endpoint,
OAuth token and registration, and the metadata. The account and consent pages
are browser pages behind the MyAuth sign-in; they can stay on a private host,
and the OAuth metadata points the clients' browsers there
(`authorization_endpoint` = `UI_URL`). Unset, everything collapses onto the
host of the request.

## Configuration

Everything is an environment variable; [.env.example](../.env.example) lists them.

| Variable | Example | Used for |
|---|---|---|
| `MYAUTH_APP_KEY` | random, secret | this app's key in MyAuth; fetching the users' Garmin access tokens. The server refuses to start without it |
| `MYAUTH_URL` | `http://myauth:8000` | MyAuth as the container reaches it (internal network) |
| `MYAUTH_PUBLIC_PORT` | `1000` | port of MyAuth's browser pages, on the host the browser used |
| `MYAUTH_APP` | `mcp-garmin` | this app's key in MyAuth |
| `PUBLIC_URL` | `https://mcp.example.com/garmin` | OAuth issuer, token and registration endpoints |
| `MCP_URL` | the same | connector address, protected-resource metadata |
| `UI_URL` | `https://home.example.com:1030` | `authorization_endpoint` - the consent page, a browser page |
| `FOOTER_NOTE` | `Private service · Owner: ...` | optional text after the version in the page footer |
| `TZ` | `Europe/Berlin` | fallback only; each user's time zone comes from MyAuth |

Also set by the image: `MCP_DB` (the SQLite file) and `GARMIN_MCP_CACHE` (the FIT cache),
both in the volume `/data`.

## Container

[docker-compose.yml](../docker-compose.yml) builds the image and keeps `/data`
in a volume. It has no host port: put it behind your reverse proxy, in the
same Docker network as MyAuth. A `docker-compose.override.yml` next to it
(compose reads it automatically) adds that network, e.g.:

```yaml
services:
  mcp-garmin:
    networks:
      - myauth_net

networks:
  myauth_net:
    external: true
```

Then:

```bash
cp .env.example .env        # fill in MYAUTH_APP_KEY and the addresses
docker compose up -d --build
```

Updating is the same command after pulling the new code.

## Reverse proxy

Private site (account and consent pages), Caddy as an example:

```
home.example.com:1030 {
	reverse_proxy mcp-garmin:8000
	encode gzip
}
```

Public site, when the server lives under a path prefix (`/garmin`) on a host
shared with other MCP servers:

| Public path | To the app | Why |
|---|---|---|
| `/garmin` | `/mcp` | the MCP endpoint (the connector address) |
| `/garmin/oauth/token`, `/garmin/oauth/register` | same, prefix stripped | code exchange, refresh, client registration |
| `/garmin/.well-known/oauth-*` | same, prefix stripped | metadata, for clients that append |
| `/.well-known/oauth-protected-resource/garmin` | `/.well-known/oauth-protected-resource` | RFC 9728 location (path inserted) |
| `/.well-known/oauth-authorization-server/garmin` | `/.well-known/oauth-authorization-server` | RFC 8414 location (path inserted) |

The two `.well-known` forms with the path after `.well-known` are what lets
several MCP servers share one host, each with its own metadata. Everything
else - account page, consent page - should answer 404 on the public host.

Check afterwards, from anywhere:
`https://mcp.example.com/.well-known/oauth-protected-resource/garmin` shows the
metadata, `https://mcp.example.com/garmin` answers 401 (no token),
`https://mcp.example.com/garmin/account` answers 404.

## Data and backup

Everything is in the volume: `app.db` (users, OAuth clients and tokens) and
`cache/<uid>/` (FIT files, at most 40 per user). The Garmin connections are
MyAuth's. Back up the volume and the `.env` with `MYAUTH_APP_KEY`.

## Who may use it

Only MyAuth decides: a user needs the app enabled there. Revoking it or
blocking the user cuts their connectors off within the 30-second cache. There
is nothing to administer on this server; each user can delete their own data
on the account page.

## Troubleshooting

- **Account page says "Sign-in unavailable", MCP answers 503:** MyAuth is down
  or not in the server's network (`MYAUTH_URL`).
- **The consent page does not load when adding a connector:** the browser
  cannot reach `UI_URL` or MyAuth - in a private setup, the device is not on
  the LAN or VPN.
- **Connectors suddenly ask to sign in again:** the user lost the app in MyAuth,
  or MyAuth's user for that uid changed (a rebuilt MyAuth database). In the
  second case this server drops the old owner's data on purpose.
- **Everything public answers nothing:** check the public host and the reverse
  proxy routes above.
- **Startup fails with "account-based version":** the volume holds a database
  from 0.1 (own accounts). Start with an empty volume.
