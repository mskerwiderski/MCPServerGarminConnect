"""access v1.4.0 - sign-in, access checks, platform access and AI runs through MyAuth.

A MyCommons building block, copied into its users by MyCommons/sync.sh:
Garmin MCP (garmin_mcp/access/), Libre MCP (libre_mcp/access/),
MyDocContainer's MCP and MyFITContainer's MCP (both app/mcp/access/), and the
web apps MyGPXContainer, PKVAgent, MyDocContainer and MyFITContainer
(app/access/). Never edit a copy;
change it in MyCommons, run its tests, bump the version, sync again.

`access.myauth` talks to MyAuth (see its docstring); `access.web` turns a
browser request into a sign-in outcome for web apps; `access.ai` runs AI
through MyAuth (model list, runs with the subscription fallback, parsing).
"""

__version__ = "1.4.0"
