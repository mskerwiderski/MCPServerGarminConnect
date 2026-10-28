# Standalone on your own machine

No server, no MyAuth, no accounts: the server runs on your own computer for
one person and Claude Desktop or Claude Code starts it there. You sign in to
Garmin once in a terminal; after that the tools work like the remote connector.

```
Claude Desktop / Claude Code ── starts ──▶ garmin-mcp stdio ──▶ Garmin Connect
                                               │
                                               └─ ~/.config/garmin-mcp/tokens.json
```

## 1. Install

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/mskerwiderski/MCPServerGarminConnect
garmin-mcp --help
```

`uv tool install` puts `garmin-mcp` into `~/.local/bin` (run
`uv tool update-shell` once if that is not on your `PATH`). To update, run the
same command with `--reinstall`. From a local clone, pass its path instead of
the URL (`uv tool install --reinstall ./MCPServerGarminConnect`).

## 2. Sign in to Garmin

```bash
garmin-mcp login
```

It asks for your Garmin e-mail, your password and - if your account uses
two-step verification - the code Garmin sends you. A wrong code may be
retried; each attempt makes Garmin send a new one, so always enter the newest.

```bash
garmin-mcp status      # who is signed in, until when the access token is valid
garmin-mcp logout      # delete the tokens again
```

Run the sign-in on your own computer: Garmin's sign-in refuses many requests
from data-centre addresses (rate limit, bot protection).

## 3. Add it to Claude

**Claude Code** (this only reaches Claude Code, not Claude Desktop):

```bash
claude mcp add --scope user garmin -- garmin-mcp stdio
claude mcp list              # garmin ... Connected
```

`--scope user` makes it available in every directory; without it Claude Code
only offers it in the directory you ran the command in.

**Claude Desktop** (a separate configuration): Settings → Developer → Edit Config, then in
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "/Users/<you>/.local/bin/garmin-mcp",
      "args": ["stdio"]
    }
  }
}
```

Claude Desktop needs the full path (`which garmin-mcp` prints it) and a
complete restart (Cmd+Q, not just closing the window). Settings → Developer
then lists the server with its status; if it failed, the reason is in
`~/Library/Logs/Claude/mcp-server-garmin.log`. A syntax error in the JSON file
makes Claude Desktop ignore it silently - check it with
`python3 -m json.tool < ~/Library/Application\ Support/Claude/claude_desktop_config.json`.

Ask "Which Garmin account are you connected to?" to check (the `whoami` tool).

## What is stored, and where

- **`~/.config/garmin-mcp/tokens.json`** (or `$XDG_CONFIG_HOME/garmin-mcp/`, or
  the path in `GARMIN_MCP_TOKENS`): the OAuth1 token (valid for about a year),
  the current OAuth2 access token and your Garmin display name. The file is
  readable by you only (0600, directory 0700). Your password is never stored.
- The access token is short-lived; the server renews it from the OAuth1
  token by itself, ten minutes before it expires, without signing in again. When Garmin refuses that (the
  OAuth1 token expired or was revoked), the tools say so: run
  `garmin-mcp login` again.
- **`~/.garmin-mcp/cache/`** (or `GARMIN_MCP_CACHE`): the last 40 FIT files
  downloaded, so asking about the same activity twice does not download it
  twice.

Anyone who can read `tokens.json` can read your Garmin data - treat it like a
password. `garmin-mcp logout` deletes it; to cut it off on Garmin's side as
well, change your Garmin password.

## Removing it again

```bash
claude mcp remove garmin --scope user     # Claude Code (without --scope: in the
                                          # directory where you added it)
garmin-mcp logout                         # delete the tokens
rm -r ~/.garmin-mcp                       # the FIT cache
uv tool uninstall garmin-mcp              # the program
```

For Claude Desktop, delete the `"garmin"` entry from its config file.

## How it relates to the hosted mode

Same tools, same code. The difference is only where the Garmin access comes
from: in the [hosted mode](hosted.md) from MyAuth (`garmin-mcp serve`), here
from the token file (`garmin-mcp stdio`). Signing in and renewing come from
`garmin_mcp/garmin_login/`, a vendored building block that MyAuth uses as
well (see [architecture.md](architecture.md#vendored-code)).
