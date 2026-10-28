# Installing for Claude

Two routes:

- **Standalone, on your own computer** - Claude Desktop or Claude Code starts
  `garmin-mcp stdio` locally. Anyone can use this; no server, no account.
  Full guide: [standalone.md](standalone.md). In short:

  ```bash
  uv tool install git+https://github.com/mskerwiderski/MCPServerGarminConnect
  garmin-mcp login
  claude mcp add --scope user garmin -- garmin-mcp stdio
  ```

- **Hosted, as a remote connector** - for claude.ai in the browser and the
  phone app. Needs a hosted server and your account there (MyAuth account with
  the app "Garmin MCP", Garmin connected in MyAuth): [account.md](account.md).
  The steps below use the placeholder `https://mcp.example.com/garmin`;
  use your server's connector address.

---

## claude.ai (browser and phone app)

1. **Settings → Connectors → Add custom connector**.
2. Paste the connector address, e.g. `https://mcp.example.com/garmin`.
3. Claude registers itself and sends you to the server's consent page. You are
   signed in through MyAuth (sign in there first if asked) - click **Allow**.
4. The connector appears in your list. Open a new chat and ask something.

The connector also shows up in the Claude phone app and in Claude Desktop once
it is added on claude.ai.

## Claude Code (hosted)

```bash
claude mcp add --transport http garmin https://mcp.example.com/garmin
```

Run `/mcp` in a session afterwards to complete the OAuth login (consent page as
above). MCP servers are loaded when a session starts - start a new one.

---

## Troubleshooting

**"not signed in to Garmin yet - run `garmin-mcp login` …"** (standalone)
Run `garmin-mcp login` in a terminal on the same computer, then ask again.

**"your Garmin account is not connected yet"** (hosted)
The server is reachable, but your MyAuth account has no Garmin connection.
Connect Garmin in MyAuth (the account page links to it). The connector keeps
working afterwards - nothing to re-add.

**"Garmin refused to renew the access"**
Garmin ended the session, or the password was changed. Standalone: run
`garmin-mcp login` again. Hosted: disconnect and connect Garmin again in MyAuth.

**The consent page does not load when adding the connector** (hosted)
It signs you in through MyAuth. If MyAuth is private, your device has to be on
its network or VPN - connect and try again. *No access* there means the app
"Garmin MCP" is not enabled for your MyAuth account.

**Sign-in to Garmin fails with 429, 403, or a mention of Cloudflare**
Garmin is blocking the address the sign-in comes from. From a home connection
this is usually temporary - wait a few minutes and try again. From a data
centre it often does not work at all.

**"MFA code rejected"**
Codes expire quickly. Start the Garmin sign-in again to get a fresh one.

**Everything works but answers are empty**
Ask Claude to call `whoami`. It reports which Garmin account is connected. An
empty result with a working connection usually means the data really is not
there - Garmin files sleep under the morning you woke up, and training readiness
only exists once the watch has synced after waking.
