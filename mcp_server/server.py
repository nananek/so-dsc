"""MCP server exposing the so-dsc camera as tools.

Stdio MCP server that proxies to the existing Flask API. The Flask side
(see ../run.py) must be running and reachable at SODSC_API_BASE.

Why a thin proxy: keeps the camera-side state in one place. Flask owns
the persistent SonyClient (single liveview consumer, watchdog, etc.); the
MCP server is stateless and can be relaunched freely by the AI client.

Run from CLI:

    python -m mcp.server
    SODSC_API_BASE=http://127.0.0.1:5050 python -m mcp.server
"""
from __future__ import annotations

import json
import os
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP, Image

API_BASE = os.environ.get("SODSC_API_BASE", "http://127.0.0.1:5050").rstrip("/")
HTTP_TIMEOUT = float(os.environ.get("SODSC_HTTP_TIMEOUT", "30"))

mcp = FastMCP("so-dsc")

_session = requests.Session()


def _get(path: str, **params) -> Any:
    r = _session.get(f"{API_BASE}{path}", params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict | None = None) -> Any:
    r = _session.post(
        f"{API_BASE}{path}",
        json=body or {},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _get_bytes(path: str, **params) -> bytes:
    r = _session.get(f"{API_BASE}{path}", params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.content


# ---------- introspection ----------

@mcp.tool()
def get_status() -> str:
    """Report current camera state: liveview running, last frame age, mode,
    available APIs, battery, recording status. Use this first when planning
    a shoot to know what controls are exposed.
    """
    return json.dumps(_get("/api/status"), indent=2)


@mcp.tool()
def reconnect() -> str:
    """Force a full reconnect to the camera. Use after a power-cycle or
    when get_status reports stale data (idle_seconds growing, frames not
    advancing). Idempotent.
    """
    return json.dumps(_post("/api/reconnect"), indent=2)


# ---------- liveview (vision) ----------

@mcp.tool()
def get_liveview_frame() -> Image:
    """Return the latest live preview JPEG (~640x424, ~35KB on RX100M5A).
    Use this to *see* what the camera is currently aimed at — e.g. to
    judge composition or exposure before triggering a shot.
    """
    data = _get_bytes("/snapshot.jpg")
    return Image(data=data, format="jpeg")


# ---------- shooting ----------

@mcp.tool()
def take_picture(save: bool = True) -> Image:
    """Trigger the shutter and return the postview JPEG (down-sampled to
    ~1616x1080). The full-resolution original is kept on the camera SD
    card and is reachable via the in-camera "Send to Smartphone" mode if
    you need it later.

    save=True (default): the postview is also saved on the host under
    downloads/. False: only return the image, don't persist.
    """
    resp = _post("/api/shoot", {"save": bool(save)})
    urls = resp.get("postview") or []
    if not urls:
        raise RuntimeError(f"shoot returned no postview: {resp}")
    # Fetch the postview image bytes through the proxy. If the saved-copy
    # path is in the response, prefer it (the postview URL is short-lived).
    saved = resp.get("saved")
    if saved and saved.get("name"):
        data = _get_bytes(f"/downloads/{saved['name']}")
    else:
        data = _get_bytes("/api/content/proxy", url=urls[0])
    return Image(data=data, format="jpeg")


@mcp.tool()
def half_press(on: bool) -> str:
    """Half-press (or release) the shutter button. Use on=True to make the
    camera focus at the body's currently-selected Focus Area, on=False to
    release. Useful as a "lock focus" before take_picture in tricky AF
    situations. Note: setTouchAFPosition is *not* supported on RX100M5A
    Smart Remote Control v2.1.7 — AF *position* is set on the camera body.
    """
    return json.dumps(_post("/api/half_press", {"on": bool(on)}))


@mcp.tool()
def zoom(direction: str, movement: str = "1shot") -> str:
    """Zoom the lens.
    direction: "in" (telephoto) or "out" (wide).
    movement: "1shot" (one click), "start" (continuous, hold until "stop"),
              "stop" (release continuous zoom).
    """
    if direction not in ("in", "out"):
        raise ValueError("direction must be 'in' or 'out'")
    if movement not in ("1shot", "start", "stop"):
        raise ValueError("movement must be '1shot' | 'start' | 'stop'")
    return json.dumps(_post("/api/zoom", {"direction": direction, "movement": movement}))


# ---------- exposure ----------

@mcp.tool()
def set_iso(value: str) -> str:
    """Set ISO. Pass a string the camera advertises — typically "AUTO",
    "100", "200", ..., "12800". Use get_status → available_apis +
    getEvent state to confirm what the camera will accept.
    """
    return json.dumps(_post("/api/setting", {"name": "iso", "value": value}))


@mcp.tool()
def set_shutter(value: str) -> str:
    """Set shutter speed. Strings like "1/250", "1/4", "2", "BULB".
    """
    return json.dumps(_post("/api/setting", {"name": "shutter", "value": value}))


@mcp.tool()
def set_fnumber(value: str) -> str:
    """Set aperture (f-number) as a string like "5.6". Only honored in
    Aperture-priority or Manual mode."""
    return json.dumps(_post("/api/setting", {"name": "fnumber", "value": value}))


@mcp.tool()
def set_exposure_compensation(steps: int) -> str:
    """Exposure compensation in 1/3 EV steps (typical range: -9 to +9, =
    -3.0 to +3.0 EV)."""
    return json.dumps(_post("/api/setting", {"name": "ev", "value": int(steps)}))


@mcp.tool()
def set_white_balance_auto() -> str:
    """Switch white balance to Auto WB."""
    return json.dumps(_post("/api/wb", {"mode": "Auto WB"}))


@mcp.tool()
def set_white_balance_kelvin(temperature_k: int) -> str:
    """Lock white balance to a specific color temperature in kelvin
    (e.g. 5500 for daylight, 3200 for tungsten). Camera typically accepts
    multiples of 100 in 2500..9900."""
    return json.dumps(_post("/api/wb", {"kelvin": int(temperature_k)}))


# ---------- burst / bulb ----------

@mcp.tool()
def start_burst() -> str:
    """Start continuous (burst) shooting. Camera must already be in still
    shoot mode with continuous drive set on the body. Stop with stop_burst.
    Returns immediately while the camera keeps shooting.
    """
    return json.dumps(_post("/api/burst/start"))


@mcp.tool()
def stop_burst() -> str:
    """Stop a running burst. Returns the list of postview URLs (one per
    frame the camera captured). Pull individual ones via get_liveview_frame
    won't work — use the URLs as-is or wait and use list_saved_pictures
    after the camera has finished writing them."""
    return json.dumps(_post("/api/burst/stop"))


@mcp.tool()
def start_bulb() -> str:
    """Open the shutter for bulb exposure. Camera must be in Manual
    exposure mode with shutter speed = "BULB". Pair with stop_bulb."""
    return json.dumps(_post("/api/bulb/start"))


@mcp.tool()
def stop_bulb() -> str:
    """Close the bulb shutter."""
    return json.dumps(_post("/api/bulb/stop"))


# ---------- saved pictures ----------

@mcp.tool()
def list_saved_pictures() -> str:
    """List pictures saved on the host (downloads/ directory). These are
    the postview JPEGs from take_picture(save=True), plus anything pulled
    via /api/content/download."""
    return json.dumps(_get("/api/content/downloaded"), indent=2)


@mcp.tool()
def get_saved_picture(name: str) -> Image:
    """Return a previously-saved picture (file under downloads/) as an
    Image. Use list_saved_pictures to find the name."""
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("unsafe filename")
    data = _get_bytes(f"/downloads/{name}")
    return Image(data=data, format="jpeg")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
