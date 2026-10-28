"""access v1.2.1 - sign-in, access checks and platform access through MyAuth.

A MyCommons building block, copied into its users by MyCommons/sync.sh:
Garmin MCP (garmin_mcp/access/), Libre MCP (libre_mcp/access/),
MyDocContainer's MCP and MyFITContainer's MCP (both app/mcp/access/), and the
web apps as they switch (MyGPXContainer and PKVAgent: app/access/). Never edit a copy;
change it in MyCommons, run its tests, bump the version, sync again.

`access.myauth` talks to MyAuth (see its docstring); `access.web` turns a
browser request into a sign-in outcome for web apps.
"""

__version__ = "1.2.1"
