# Installing for Mistral Vibe (formerly Le Chat)

Mistral renamed Le Chat to **Vibe** during 2026; older articles, and the app
listing for a while, still say Le Chat. It is the same product, and existing
accounts, conversations and plans carried over.

Vibe can talk to any remote MCP server, so this route uses a **hosted**
server ([hosted.md](hosted.md)) and your account there. The steps use the
placeholder `https://mcp.example.com/garmin`; use your server's connector
address.

It cannot start a program on your computer, so the local route in
[install-claude.md](install-claude.md) does not apply here.

## Why this works without any special handling

Mistral's custom connectors speak **streamable HTTP** and support **OAuth 2.1
with dynamic client registration** - which is exactly what this server offers.
There is nothing to configure beyond the address: the app detects the
authentication method itself and walks you through the consent screen.

## Where it runs

| | Custom MCP connectors |
|---|---|
| Browser (chat.mistral.ai) | yes |
| iOS and Android app | yes, including adding them |
| macOS | no native app - use the browser |

Unlike ChatGPT, the phone app is not a second-class citizen here: connectors can
be added and used from it.

## Before you start

- Your account on the hosted server set up, with Garmin connected:
  [account.md](account.md). That also needs a device that can reach MyAuth.
- Permission to add connectors. Adding one is an administrator function; on
  personal plans you are the administrator of your own workspace. In a team
  workspace, an administrator has to add it.

## 1. - 2. Account and Garmin

All in [account.md](account.md).

## 3. Add the connector

**Connectors → + Add Connector → Custom MCP Connector**. If the entry is not in the main navigation, open the sidebar and look under **Intelligence**.

| Field | Value |
|---|---|
| Connector name | `garmin` - no spaces, no special characters |
| Server URL | `https://mcp.example.com/garmin` (your connector address) |
| Description | `My Garmin Connect data: activities, sleep, HRV, training status` |

Save. Mistral detects that the server uses OAuth and sends you to its consent
page, where you are signed in through MyAuth - click **Allow**. If MyAuth is
private, the phone has to be on its network or VPN for this step.

## 4. Use it

Enable the connector for your conversation and ask:

> What were my last five runs, with distance, pace and average heart rate?

More ideas in [usage.md](usage.md).

## Limitations on Mistral's side

These come from the custom connector implementation, not from this server:

- **No dynamic tool discovery.** When this server gains new tools, Mistral will
  not pick them up on its own - remove the connector and add it again.
- **No resources and no prompt templates.** Only tools are used. Nothing here
  depends on either, so this costs you nothing today.

## Troubleshooting

**"Could not connect" when saving**
The most common cause is the address. It must include its path (e.g.
`/garmin`) - the host alone does not answer the MCP handshake. Check in a
browser that the protected-resource metadata, e.g.
`https://mcp.example.com/.well-known/oauth-protected-resource/garmin`, shows a
small JSON document with `"resource"`.

**The connector name is rejected**
Mistral wants a single word without spaces or special characters. `garmin`
works, `Garmin Connect` does not.

**The OAuth window opens but does not load**
The consent page signs you in through MyAuth. If MyAuth is private, your
device has to be on its network or VPN - connect and try again.

**Every answer says "your Garmin account is not connected yet"**
Garmin is not connected yet. Open the server's account page (e.g.
`https://home.example.com:1030/account`); it must show the Garmin account as
connected.
