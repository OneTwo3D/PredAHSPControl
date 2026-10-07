"""Minimal **read-only** Home Assistant API client (REST + WebSocket).

Configuration via environment:

* ``HA_URL``   e.g. ``https://homeassistant.example.org`` (no trailing slash)
* ``HA_TOKEN`` long-lived access token, or ``HA_TOKEN_FILE`` pointing to a file containing it

Only an explicit allowlist of read-only WebSocket commands and REST GET paths can be sent; anything
else raises ``PermissionError``. The token is never logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
from typing import Any

READ_ONLY_WS_TYPES = frozenset(
    {
        "get_states",
        "get_config",
        "recorder/info",
        "recorder/list_statistic_ids",
        "recorder/get_statistics_metadata",
        "recorder/statistics_during_period",
        "history/history_during_period",
    }
)
# Exact read-only REST endpoints. A prefix such as "/api/" would also admit GET webhooks
# (/api/webhook/<id>), which can trigger automations.
READ_ONLY_REST_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"/api/",
        r"/api/config",
        r"/api/states",
        r"/api/states/[a-z_]+\.[a-z0-9_]+",
        r"/api/history/period(/[0-9T:.+\-Z]+)?",
        r"/api/config/automation/config/[A-Za-z0-9_]+",
    )
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib would resend the Authorization header to the new location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise PermissionError(f"redirect to {newurl} refused (would forward the token)")


def _path_allowed(path: str) -> bool:
    return any(p.fullmatch(path) for p in READ_ONLY_REST_PATTERNS)


def _token() -> str:
    tok = os.environ.get("HA_TOKEN")
    if not tok and os.environ.get("HA_TOKEN_FILE"):
        with open(os.environ["HA_TOKEN_FILE"], encoding="utf-8") as fh:
            tok = fh.read()
    if not tok:
        raise RuntimeError("set HA_TOKEN or HA_TOKEN_FILE")
    return tok.strip()


def _base_url() -> str:
    url = os.environ.get("HA_URL", "").rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise RuntimeError("set HA_URL, e.g. https://homeassistant.example.org")
    return url


def rest_get(path: str, params: dict[str, str] | None = None, timeout_s: float = 120) -> Any:
    """GET a read-only REST endpoint and return decoded JSON."""
    if not _path_allowed(path):
        raise PermissionError(f"REST path not allowed: {path}")
    url = _base_url() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_token()}"}, method="GET")
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    with opener.open(req, timeout=timeout_s) as resp:
        return json.loads(resp.read())


async def _ws_calls(messages: list[dict[str, Any]]) -> list[Any]:
    for m in messages:
        if m.get("type") not in READ_ONLY_WS_TYPES:
            raise PermissionError(f"WebSocket command not allowed: {m.get('type')}")
    import websockets  # imported lazily: optional tools dependency

    ws_url = _base_url().replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    results: list[Any] = []
    async with websockets.connect(ws_url, max_size=2**28, open_timeout=30) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": _token()}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise PermissionError("Home Assistant authentication failed")
        for i, msg in enumerate(messages, start=1):
            await ws.send(json.dumps({**msg, "id": i}))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == i and reply.get("type") == "result":
                    if not reply.get("success"):
                        raise RuntimeError(f"{msg['type']} failed: {reply.get('error')}")
                    results.append(reply["result"])
                    break
    return results


def ws_calls(messages: list[dict[str, Any]]) -> list[Any]:
    """Send read-only WebSocket commands sequentially and return their results."""
    return asyncio.run(_ws_calls(messages))
