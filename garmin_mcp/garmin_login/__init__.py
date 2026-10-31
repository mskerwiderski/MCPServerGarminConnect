"""garmin_login v1.1.0 - sign in to Garmin Connect, renew the access, and
keep both for many users.

A MyCommons building block, copied into its users by MyCommons/sync.sh:
Garmin MCP (garmin_mcp/garmin_login/, standalone mode), MyAuth
(app/garmin_login/), MyGPXContainer (app/garmin_login/, own accounts).
Never edit a copy; change it in MyCommons, run its tests, bump the version,
sync again. Garmin's protocol is `garmin_login.signin`; waiting sign-ins,
sealed tokens and renewal per user are `garmin_login.keeper` (since 1.1.0,
needs `cryptography` for its Sealer). See their docstrings.
"""

__version__ = "1.1.0"
