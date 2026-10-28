# garmin-mcp

**Talk to your Garmin data.** A read-only [MCP](https://modelcontextprotocol.io)
server that lets Claude (and, in the hosted mode, ChatGPT and Mistral Vibe)
answer questions about your training:
activities, sleep, HRV, Body Battery, training status, challenge leaderboards -
and, unlike other Garmin MCP servers, the **original FIT file** your watch
wrote, including Connect-IQ sensor channels (Stryd, Moxy/SmO2, CORE).

```
You:  How did my last long run compare to the same route in June?
You:  Why is my training readiness so low this morning?
You:  Where do I stand in the running challenge, and who is ahead of me?
You:  Did my Stryd actually record power on Tuesday's session?
```

It never writes to Garmin Connect. Every tool is read-only.

---

## Quick start: on your own computer

The standalone mode is for one person: Claude Desktop or Claude Code starts the
server on your machine, and it talks to Garmin Connect with your own tokens.
No server, no accounts. You need Python 3.12 or newer and
[uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/mskerwiderski/MCPServerGarminConnect
garmin-mcp login                                   # Garmin e-mail, password, MFA code
claude mcp add --scope user garmin -- garmin-mcp stdio
```

Then ask Claude "Which Garmin account are you connected to?". Claude Desktop,
where the tokens are stored, updating and removing:
[docs/standalone.md](docs/standalone.md).

## Hosted: many users, any AI client

`garmin-mcp serve` runs the same tools as a remote MCP server with its own
OAuth 2.1 authorization server, so claude.ai (browser and phone), ChatGPT and
Mistral Vibe can add it as a connector. It is multi-tenant: each user's
connector only ever sees that user's Garmin data.

Users sign in through **MyAuth**, the author's self-hosted login service. It
decides who may use the server and holds the Garmin connections. **MyAuth is
not published**, so the hosted mode will not run without it (or a service
answering the same few endpoints, listed in [docs/hosted.md](docs/hosted.md)).
It is documented here for transparency and for the author's own instance;
for yourself, use the standalone mode above.

## Documentation

| Guide | What is in it |
|---|---|
| [Standalone on your own machine](docs/standalone.md) | `garmin-mcp login`, Claude Desktop / Claude Code over stdio, the token file |
| [Install for Claude](docs/install-claude.md) | Standalone and hosted, claude.ai, Claude Code, troubleshooting |
| [Install for ChatGPT](docs/install-chatgpt.md) | Hosted only: developer mode, custom connector, troubleshooting |
| [Install for Mistral Vibe](docs/install-mistral.md) | Hosted only: custom MCP connector, OAuth, troubleshooting |
| [Hosted: your account](docs/account.md) | MyAuth, connecting Garmin, the connector address |
| [Usage examples](docs/usage.md) | What to ask, what comes back, what the numbers mean |
| [Tool reference](docs/tools.md) | Every tool, its parameters and an example response |
| [Hosted: running the server](docs/hosted.md) | Requirements, configuration, reverse proxy, container |
| [How it works](docs/architecture.md) | Design decisions, and the Garmin quirks behind them |

## What it can answer

**Activities** - list and filter by sport and date, Garmin's own summary of one
activity, and the same activity as the device recorded it, straight from the
FIT file. Time series for any channel in the file, pool swim detail down to the
single length, and which Connect-IQ sensors actually delivered data.

**Health** - one call per day gives steps, resting heart rate, stress, Body
Battery, sleep phases with score, HRV status and training readiness. A night
in detail: the order of the sleep phases, the restless moments, and movement,
heart rate, HRV, stress, Body Battery, SpO2 and respiration on one time grid,
in the local time of that night. Plus training status with load focus and VO2max, body composition and blood pressure
over a date range.

**Trends and plan** - a whole month of steps, Body Battery and VO2max in one
call instead of one per day; the scheduled side of Garmin with training-plan
workouts and races, so "what was planned" and "what happened" can be compared.

**Where you stand** - race predictions for 5k to marathon, fitness age,
endurance and hill score, lifetime totals, and your personal records with the
activity that set each one.

**Context** - your thresholds and heart rate zones, your gear with the
activities, kilometres, hours and days behind it, and the social challenges you run against friends, including the one
currently in progress.

See [docs/tools.md](docs/tools.md) for the full list with parameters.

## Where the Garmin login happens

**Standalone:** on your own computer, with `garmin-mcp login`. Your password is
used once to obtain Garmin's tokens and is stored nowhere; the tokens go to
`~/.config/garmin-mcp/tokens.json`, readable by you only (0600). The server
renews the short-lived access token by itself.

**Hosted:** in MyAuth, the one place that signs in to Garmin. It keeps the
long-lived OAuth1 token (valid for about a year, never handed out) and gives
this server short-lived access tokens only. When Garmin changes its sign-in,
it is fixed there - once, for every app.

Either way, run the sign-in from a home connection: Garmin's SSO sits behind
Cloudflare, which since March 2026 answers fresh logins from datacenter
addresses with 429 or 403.

## Good to know

This uses Garmin's **internal** Connect API - the same one every community tool
uses. There is no official API for individuals; Garmin's Health API is a partner
programme. Use this for your own account and for people you know: all users on
one hosted server share one address towards Garmin.

Responses are deliberately small. Garmin answers with hundreds of fields per
activity and time series of thousands of points; everything is projected down to
what a model can actually use, and streams are resampled with min/max/avg per
channel. That is not cosmetic - the raw payloads would eat the context window
the conversation needs.

## Development

```bash
uv sync --extra dev
```

```bash
uv run pytest
```

105 tests, no network access required - the Garmin API, MyAuth and the FIT files
are stubbed or synthesised. See [docs/architecture.md](docs/architecture.md) for how
the pieces fit together. Some folders are vendored copies of shared building
blocks and are not edited here; the architecture document says which.

## Disclaimer

An independent, unofficial project, not affiliated with or endorsed by Garmin.
It uses Garmin Connect's internal API, which Garmin can change or close at any
time; when it does, tools may fail until this project catches up. Use it for
your own account. Garmin and Garmin Connect are trademarks of Garmin Ltd. or
its subsidiaries.

## License

MIT. See [LICENSE](LICENSE).
