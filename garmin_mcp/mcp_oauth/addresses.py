"""The addresses of an MCP server behind MyAuth - part of the MyCommons
building block `mcp_oauth`.

A hosted server can have two faces:
  public_url  e.g. https://mcp.example.com/garmin - OAuth issuer, token and
              registration endpoints; public for the AI clients
  mcp_url     the connector address; often the same URL, mapped to /mcp
  ui_url      e.g. https://home.example.com:1030 - account and consent pages;
              may stay private
Unset, everything collapses onto the origin of the request (one host).

By default the three come from the env vars PUBLIC_URL, MCP_URL and UI_URL. A
host whose settings use other names passes callables to `configure` (they
return the URL or an empty value).
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

_public_url = None        # callable or None: then PUBLIC_URL
_mcp_url = None           # callable or None: then MCP_URL
_ui_url = None            # callable or None: then UI_URL

_UNSET = object()


def configure(*, public_url=_UNSET, mcp_url=_UNSET, ui_url=_UNSET) -> None:
    """Callables for hosts whose settings are not PUBLIC_URL/MCP_URL/UI_URL;
    None goes back to the env var, an omitted argument stays as it is."""
    global _public_url, _mcp_url, _ui_url
    if public_url is not _UNSET:
        _public_url = public_url
    if mcp_url is not _UNSET:
        _mcp_url = mcp_url
    if ui_url is not _UNSET:
        _ui_url = ui_url


def _setting(source, env: str) -> str:
    value = source() if source else os.environ.get(env, "")
    return (value or "").rstrip("/")


def request_origin(request) -> str:
    """The origin this browser used - a private host name, a LAN IP, or the
    single host of a simple setup."""
    proto = request.headers.get("x-forwarded-proto", "https")
    return f"{proto}://{request.headers.get('host', 'localhost')}"


def public_base(request) -> str:
    return _setting(_public_url, "PUBLIC_URL") or request_origin(request)


def mcp_url(request) -> str:
    return _setting(_mcp_url, "MCP_URL") or f"{public_base(request)}/mcp"


def ui_base(request) -> str:
    return _setting(_ui_url, "UI_URL") or public_base(request)


def well_known(url: str, name: str) -> str:
    """RFC 8414 / RFC 9728: /.well-known/<name> goes between host and path,
    so every app behind one public host keeps its own metadata."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/.well-known/{name}{p.path.rstrip('/')}"
