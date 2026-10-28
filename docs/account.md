# Hosted: your account

This is for users of a **hosted** server (`garmin-mcp serve`). Running it on
your own computer instead? See [standalone.md](standalone.md) - no account
needed there.

A hosted server usually has two addresses; your administrator tells you
yours. The examples below use these placeholders:

| | Address (example) | Reachable |
|---|---|---|
| Connector (for the AI clients) | `https://mcp.example.com/garmin` | from anywhere |
| Account and consent pages | `https://home.example.com:1030` | possibly only on the private network |

There is no sign-up and no password of its own: you sign in with **MyAuth**,
the login service the server is run with. This is the setup every AI client
needs once, before adding the connector.

## Before you start

- A **MyAuth account with the app "Garmin MCP" enabled.** An administrator
  switches it on in MyAuth. Without it the account page says *No access*.
- A **device that can reach MyAuth** and the account page. In a private setup
  that means being on the home network or connected through its VPN. Only the
  connector address has to be public - that is what lets claude.ai, ChatGPT
  and Mistral reach it.

## 1. Open the account page

Go to `https://home.example.com:1030/account`. Without a MyAuth session you are
sent to the MyAuth sign-in and come straight back.

## 2. Connect Garmin - in MyAuth

The Garmin connection lives in MyAuth, not on this server: every app that
reads Garmin uses it, and a broken Garmin sign-in is fixed there once. The
account page links to it (**Connect Garmin in MyAuth**).

Enter your Garmin e-mail and password, plus a multi-factor code if your
account uses one. The password is used once to obtain Garmin's tokens and is
stored nowhere; MyAuth keeps the tokens encrypted and hands this server only
short-lived access. The card is only there when an app you may use reads
Garmin.

This server's account page now shows the Garmin account as **connected**.

## 3. Copy the connector address

The account page shows it ready to copy, for example:

```
https://mcp.example.com/garmin
```

The same address for every client, exactly like this. When a client adds it,
it sends your browser to the consent page that asks you to **Allow** read
access - you are signed in through MyAuth there, so again: on a device that
can reach MyAuth. After that the connector works from anywhere, including the
phone apps.

Continue with your client: [Claude](install-claude.md),
[ChatGPT](install-chatgpt.md), [Mistral Vibe](install-mistral.md).

## Removing access

- **Your data:** *Delete my data* on the account page removes every connector
  authorization and the cached FIT files. Your MyAuth account and its Garmin
  connection stay - disconnect Garmin in MyAuth to cut Garmin off for every app.
- **Somebody else's access:** switch the app off for them in MyAuth, or block
  the user there. Their connectors stop working within about 30 seconds.
