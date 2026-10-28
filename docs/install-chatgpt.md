# Installing for ChatGPT

ChatGPT can only talk to a server it can reach over HTTPS - it cannot start a
program on your computer. So this route needs a **hosted** server
([hosted.md](hosted.md)) and your account there. The steps use the
placeholder `https://mcp.example.com/garmin`; use your server's connector
address.

If you want the local, no-server route, use Claude Desktop or Claude Code
instead: [install-claude.md](install-claude.md).

## Before you start

- A ChatGPT plan that offers developer mode: Plus, Pro, Business, Enterprise or
  Edu. On Business and above, a workspace admin has to allow it first.
- Your account on the hosted server set up, with Garmin connected:
  [account.md](account.md). That also needs a device that can reach MyAuth.

## 1. - 3. Account, Garmin, connector address

All in [account.md](account.md). The address looks like `https://mcp.example.com/garmin`.

## 4. Turn on developer mode

In ChatGPT: **Settings → Apps & Connectors → Advanced → Developer mode**.

The menu was called **Connectors** in earlier versions and is **Apps &
Connectors** in current ones - the same place, only renamed. Some builds label
the sub-page *Advanced settings* rather than *Advanced*.

Two things this only works in: a **browser** (chatgpt.com), and a paid plan
(Plus, Pro, Business, Enterprise, Edu). The iOS and Android apps do not offer
it, and neither do some desktop app versions.

On Business, Enterprise and Edu workspaces the switch only appears once an
admin has enabled it in **Workspace Settings → Permissions & Roles → Connected
data / custom MCP connectors**. If you cannot find the toggle, that is the usual
reason - ask your admin, or use a personal Plus or Pro account.

## 5. Add the connector

**Settings → Apps & Connectors → Create**, then fill in:

| Field | Value |
|---|---|
| Name | `Garmin` |
| Description | `My own Garmin Connect data: activities, sleep, HRV, training status` |
| MCP server URL | `https://mcp.example.com/garmin` (your connector address) |
| Authentication | OAuth |

The description matters more than it looks: the model reads it when deciding
whether a question is one this connector should answer.

Save. ChatGPT registers itself with the server and opens its consent page.
You are signed in through MyAuth (sign in there first if asked) - click
**Allow**. Do this in a browser on a device that can reach MyAuth.

## 6. Use it

Start a new chat, open the connector menu in the composer and make sure `Garmin`
is enabled for the conversation. Then ask away:

> Give me my last five runs with distance, pace and average heart rate.

> What did my sleep and HRV look like last week?

More ideas in [usage.md](usage.md).

Every tool in this connector is read-only, so you can safely let ChatGPT call
them without confirming each one. If you prefer to approve each call, that
setting sits next to the connector.

## Troubleshooting

**I cannot find Connectors in Settings at all**
Look for **Apps & Connectors** - that is what the menu is called in current
versions. If neither name is there: you are in the phone app or the desktop
app (use chatgpt.com in a browser), or you are on the Free plan, or a workspace
admin has not enabled it. See step 4.

**"Could not connect" when saving the connector**
Open the server's protected-resource metadata in a browser, e.g.
`https://mcp.example.com/.well-known/oauth-protected-resource/garmin` - it must
show a small JSON document with `"resource"`. If it does not, the server or its
public reverse proxy is the problem, not ChatGPT. Make sure you entered the
address exactly, including its path (e.g. `/garmin`).

**The OAuth window opens but does not load**
The consent page signs you in through MyAuth. If MyAuth is private, your
device has to be on its network or VPN - connect and try again. Sign in with
your MyAuth account, not with your Garmin credentials.

**The OAuth window says "No access"**
Your MyAuth account does not have the app "Garmin MCP" - an administrator
enables it in MyAuth.

**Tools appear but every answer says "your Garmin account is not connected yet"**
Garmin is not connected yet. Open the server's account page (e.g.
`https://home.example.com:1030/account`) - it must show the Garmin account as
connected.

**Answers stop working after a while**
Access tokens expire after an hour and ChatGPT refreshes them automatically. If
that fails, remove the connector and add it again. If an administrator
revoked the app in MyAuth, the connector stops working within about 30 seconds,
by design.

**The model does not use the connector**
Enable it explicitly for the conversation in the composer, or name it: "Using
the Garmin connector, show me ...". A vague description makes this worse, which
is why step 5 suggests a specific one.
