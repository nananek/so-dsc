"""Flask routes for the so-dsc viewer + remote control + content browser."""
from __future__ import annotations

import queue
from pathlib import Path

import requests
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
    stream_with_context,
)

from .content import ModeSwitchUnsupported
from .sony import SonyApiError

bp = Blueprint("sodsc", __name__)


def _client():
    return current_app.config["SONY_CLIENT"]


def _browser():
    return current_app.config["SONY_BROWSER"]


# ---------- pages ----------

@bp.route("/")
def index():
    return render_template("index.html")


# ---------- liveview ----------

@bp.route("/stream")
def stream():
    client = _client()
    q = client.subscribe()
    boundary = b"--sodsc"

    def gen():
        try:
            while True:
                try:
                    jpeg = q.get(timeout=10)
                except queue.Empty:
                    return
                yield b"".join([
                    boundary, b"\r\n",
                    b"Content-Type: image/jpeg\r\n",
                    b"Content-Length: ", str(len(jpeg)).encode(), b"\r\n\r\n",
                    jpeg, b"\r\n",
                ])
        finally:
            client.unsubscribe(q)

    return Response(
        gen(),
        mimetype="multipart/x-mixed-replace; boundary=sodsc",
        headers={"Cache-Control": "no-store"},
    )


@bp.route("/snapshot.jpg")
def snapshot():
    jpeg, _ = _client().latest_frame()
    if jpeg is None:
        abort(503, "no frame available yet")
    return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------- status / mode ----------

@bp.route("/api/status")
def api_status():
    return jsonify({
        "client": _client().stats(),
        "event": _client().event_state(),
    })


@bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Re-fetch DD.xml only — pick up service changes from a body-side
    in-camera app switch (Smart Remote ↔ Send to Smartphone) without
    tearing down liveview."""
    services = _client().refresh_services()
    return jsonify({"services": services})


@bp.route("/api/reconnect", methods=["POST"])
def api_reconnect():
    """Manually trigger a full reconnect (DD.xml refresh + restart).

    Use this after a camera power-cycle, or any time the watchdog hasn't
    picked up a stalled stream yet."""
    _client().reconnect()
    return jsonify(_client().stats())


@bp.route("/api/mode", methods=["POST"])
def api_mode():
    body = request.get_json(silent=True) or {}
    target = body.get("mode")
    if target not in ("Remote Shooting", "Contents Transfer"):
        abort(400, "mode must be 'Remote Shooting' or 'Contents Transfer'")
    try:
        if target == "Contents Transfer":
            _browser().enter_transfer_mode()
        else:
            _browser().exit_transfer_mode()
    except ModeSwitchUnsupported as e:
        # 501 Not Implemented — body has the user-facing hint about
        # switching apps on the camera.
        abort(501, str(e))
    return jsonify({"mode": target})


# ---------- shooting ----------

@bp.route("/api/shoot", methods=["POST"])
def api_shoot():
    """Take one picture.

    JSON body:
      {"save": true}    download the postview JPEG into the downloads/ dir
                        and return its name + path. Default false — useful
                        when the caller plans to fetch the postview itself
                        via /api/content/proxy."""
    body = request.get_json(silent=True) or {}
    save = bool(body.get("save"))
    try:
        urls = _client().act_take_picture()
    except SonyApiError as e:
        abort(409, str(e))
    out: dict = {"postview": urls}
    if save and urls:
        try:
            path = _browser().download(urls[0])
            out["saved"] = {
                "name": path.name,
                "path": str(path),
                "size_bytes": path.stat().st_size,
            }
        except Exception as e:  # noqa: BLE001
            # Postview URLs are short-lived; if it 404s here we still
            # succeeded at the actual shoot. Surface the issue but don't
            # turn the whole call into an error.
            out["save_error"] = str(e)
    return jsonify(out)


@bp.route("/api/wb", methods=["POST"])
def api_wb():
    """Set white balance.

    JSON body — one of:
      {"mode": "Auto WB"}                   # any wbMode the camera advertises
      {"kelvin": 5500}                       # implies mode "Color Temperature"
    """
    body = request.get_json(silent=True) or {}
    kelvin = body.get("kelvin")
    if kelvin is not None:
        try:
            params = ["Color Temperature", True, int(kelvin)]
        except (TypeError, ValueError):
            abort(400, "kelvin must be int")
    else:
        mode = body.get("mode")
        if not mode:
            abort(400, "mode or kelvin required")
        params = [str(mode), False, 0]
    try:
        _client().call("camera", "setWhiteBalance", params)
    except SonyApiError as e:
        abort(409, str(e))
    return jsonify({"params": params})


@bp.route("/api/burst/start", methods=["POST"])
def api_burst_start():
    try:
        _client().start_cont_shooting()
    except SonyApiError as e:
        abort(409, str(e))
    return jsonify({"running": True})


@bp.route("/api/burst/stop", methods=["POST"])
def api_burst_stop():
    try:
        urls = _client().stop_cont_shooting()
    except SonyApiError as e:
        abort(409, str(e))
    return jsonify({"running": False, "postview": urls})


@bp.route("/api/bulb/start", methods=["POST"])
def api_bulb_start():
    try:
        _client().start_bulb_shooting()
    except SonyApiError as e:
        abort(409, str(e))
    return jsonify({"running": True})


@bp.route("/api/bulb/stop", methods=["POST"])
def api_bulb_stop():
    try:
        _client().stop_bulb_shooting()
    except SonyApiError as e:
        abort(409, str(e))
    return jsonify({"running": False})


@bp.route("/api/movie/start", methods=["POST"])
def api_movie_start():
    """Start camera-side movie recording (records to SD card).

    Requires the camera dial to be on the movie position on RX100M5A
    (setShootMode is not exposed). Check `event.cameraStatus.cameraStatus
    == "MovieRecording"` to know it actually started.
    """
    try:
        _client().start_movie_rec()
    except SonyApiError as e:
        abort(409, str(e))
    return jsonify({"recording": True})


@bp.route("/api/movie/stop", methods=["POST"])
def api_movie_stop():
    """Stop the camera-side movie recording. Returns the postview URL
    (small JPEG thumbnail) of the just-finished movie. The actual
    movie file stays on the SD card."""
    try:
        postview = _client().stop_movie_rec()
    except SonyApiError as e:
        abort(409, str(e))
    return jsonify({"recording": False, "postview": postview})


@bp.route("/api/half_press", methods=["POST"])
def api_half_press():
    body = request.get_json(silent=True) or {}
    on = bool(body.get("on"))
    try:
        _client().half_press(on)
    except SonyApiError as e:
        abort(409, str(e))
    return jsonify({"on": on})


@bp.route("/api/zoom", methods=["POST"])
def api_zoom():
    body = request.get_json(silent=True) or {}
    direction = body.get("direction")
    movement = body.get("movement", "1shot")
    if direction not in ("in", "out"):
        abort(400, "direction must be 'in' or 'out'")
    if movement not in ("1shot", "start", "stop"):
        abort(400, "movement must be '1shot' | 'start' | 'stop'")
    try:
        _client().act_zoom(direction, movement)
    except SonyApiError as e:
        abort(409, str(e))
    return jsonify({"direction": direction, "movement": movement})


@bp.route("/api/setting", methods=["POST"])
def api_setting():
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    value = body.get("value")
    if not name:
        abort(400, "name required")
    try:
        _client().set_setting(name, value)
    except (ValueError, SonyApiError) as e:
        abort(400, str(e))
    return jsonify({"name": name, "value": value})


# ---------- content browser (avContent) ----------

@bp.route("/api/content/list")
def api_content_list():
    try:
        offset = int(request.args.get("offset", 0))
        count = int(request.args.get("count", 100))
    except ValueError:
        abort(400, "offset / count must be integers")
    return jsonify(_browser().list(offset=offset, count=count))


@bp.route("/api/content/proxy")
def api_content_proxy():
    """Stream-proxy a camera content URL through this app.

    Browsers on the user's laptop are on the camera AP, so they can reach
    the camera directly — but routing the URL through here keeps the UI
    free of mixed-host concerns and lets us add Content-Disposition for
    'download original' clicks without surprising redirects."""
    url = request.args.get("url")
    if not url:
        abort(400, "url required")
    if not (url.startswith("http://") or url.startswith("https://")):
        abort(400, "url must be http(s)")
    download = request.args.get("download") == "1"
    name = request.args.get("name")

    try:
        upstream = requests.get(url, stream=True, timeout=30)
    except requests.RequestException as e:
        abort(502, f"upstream: {e}")
    if upstream.status_code != 200:
        upstream.close()
        abort(upstream.status_code, "upstream non-200")

    headers = {}
    ctype = upstream.headers.get("Content-Type")
    if ctype:
        headers["Content-Type"] = ctype
    clen = upstream.headers.get("Content-Length")
    if clen:
        headers["Content-Length"] = clen
    if download:
        safe = (name or url.rsplit("/", 1)[-1].split("?", 1)[0] or "download").replace('"', '_')
        headers["Content-Disposition"] = f'attachment; filename="{safe}"'

    @stream_with_context
    def gen():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(gen(), headers=headers)


@bp.route("/api/content/download", methods=["POST"])
def api_content_download():
    """Save a content URL to the local downloads directory (server-side)."""
    body = request.get_json(silent=True) or {}
    url = body.get("url")
    name = body.get("name")
    if not url:
        abort(400, "url required")
    try:
        path = _browser().download(url, name)
    except (ValueError, requests.RequestException) as e:
        abort(400, str(e))
    return jsonify({"name": path.name, "path": str(path), "size_bytes": path.stat().st_size})


@bp.route("/api/content/downloaded")
def api_content_downloaded():
    return jsonify({"items": _browser().list_downloaded()})


@bp.route("/downloads/<name>")
def downloads_file(name: str):
    p: Path | None = _browser().downloaded_path(name)
    if p is None:
        abort(404)
    return send_file(p, as_attachment=True)


