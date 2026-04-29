"""Sony Camera Remote API client (JSON-RPC + Liveview binary parser).

Wire format and method list: ../docs/protocol.md
"""
from __future__ import annotations

import itertools
import json
import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from .discover import SonyDevice

log = logging.getLogger(__name__)


class SonyApiError(RuntimeError):
    """Camera returned a JSON-RPC error array."""

    def __init__(self, method: str, code: int, message: str):
        super().__init__(f"{method}: [{code}] {message}")
        self.method = method
        self.code = code
        self.message = message


@dataclass
class _Frame:
    seq: int
    timestamp_ms: int
    jpeg: bytes


@dataclass
class _Stats:
    frames_seen: int = 0
    frames_dropped: int = 0
    last_frame_at: float = 0.0
    last_seq: int = -1


class SonyClient:
    """Owns the JSON-RPC sessions, runs a background liveview reader, fans
    out JPEG frames to subscriber queues, and exposes synchronous control
    commands.

    Mirrors the QiPower client's external surface so the Flask views can
    treat both interchangeably.
    """

    def __init__(self, device: SonyDevice):
        self.device = device

        # One HTTP session, one lock — RX100M5A serializes camera-service
        # calls regardless of TCP connection. Two parallel sessions get
        # `40400 Already Polling` on the second call. We keep just one
        # session and short-poll getEvent so user calls (shoot, set_iso,
        # ...) don't fight the poller. The poll thread sleeps between calls
        # so the lock isn't held continuously.
        self._cmd_sess = requests.Session()
        self._poll_sess = self._cmd_sess  # alias kept for callsite compat
        self._cmd_lock = threading.Lock()
        self._id_iter = itertools.count(1)

        # Liveview state
        self._lv_thread: Optional[threading.Thread] = None
        self._lv_running = False
        self._lv_url: Optional[str] = None
        self._latest_frame: Optional[bytes] = None
        self._latest_seq: int = -1
        self._latest_lock = threading.Lock()
        self._subscribers: list[queue.Queue[bytes]] = []
        self._sub_lock = threading.Lock()
        self._stats = _Stats()

        # Cached state mirrored from setters / getEvent
        self._mode: Optional[str] = None  # "Remote Shooting" / "Contents Transfer"
        self._shoot_mode: Optional[str] = None
        self._battery: Optional[dict] = None
        self._available_apis: list[str] = []
        self._event_thread: Optional[threading.Thread] = None
        self._event_running = False
        self._event_state: dict = {}
        self._event_lock = threading.Lock()

        # Self-healing reconnect state
        self._reconnect_lock = threading.Lock()
        self._needs_reconnect = False
        self._wd_running = False
        self._wd_thread: Optional[threading.Thread] = None
        # Idle threshold (s) before the watchdog forces a full reconnect.
        # Tuned to be longer than the camera's "post-shutter pause" so we
        # don't reconnect after every photo.
        self._idle_reconnect_threshold = 20.0

    # ---------- lifecycle ----------

    def start(self) -> None:
        """Bring the camera into Remote Shooting and begin streaming.

        Idempotent and **tolerant of an unreachable camera at boot** — we
        always start the watchdog so the app can recover when the camera
        comes back online."""
        try:
            self.set_camera_function("Remote Shooting")
        except (SonyApiError, requests.RequestException) as e:
            # SonyApiError: e.g. "Illegal State" when already in this mode
            # or "No Such Method" on RX100M5A. RequestException: camera
            # offline at boot — watchdog will retry.
            log.info("setCameraFunction(Remote Shooting) skipped: %s", e)
        try:
            self.call("camera", "startRecMode", timeout=5.0)
        except (SonyApiError, requests.RequestException) as e:
            log.info("startRecMode skipped: %s", e)

        self._start_event_loop()
        try:
            self.start_liveview()
        except requests.RequestException as e:
            log.warning("start_liveview at boot failed: %s — watchdog will retry", e)
        # Always run the watchdog, even if boot couldn't reach the camera.
        # That way `python run.py` succeeds and the camera coming back
        # online repairs the connection automatically.
        self._needs_reconnect = not self._lv_running
        self._start_watchdog()

    def stop(self) -> None:
        self._stop_watchdog()
        self.stop_liveview()
        self._stop_event_loop()
        try:
            self.call("camera", "stopRecMode", timeout=2.0)
        except (SonyApiError, requests.RequestException) as e:
            log.debug("stopRecMode: %s", e)

    def reconnect(self) -> None:
        """Full re-init after the camera has been off / away.

        Tear down everything we held (HTTP sessions, liveview reader, event
        long-poll), re-fetch DD.xml in case the camera came back with
        different ports, then restart. Idempotent and serialized via
        _reconnect_lock so concurrent triggers (manual + watchdog) coalesce.
        """
        from .discover import from_host  # avoid import cycle at module load

        with self._reconnect_lock:
            log.info("reconnect: tearing down")
            # Stop liveview reader thread (don't bother telling the camera —
            # if it's off the stopLiveview call would just block).
            self._lv_running = False
            t = self._lv_thread
            self._lv_thread = None
            if t is not None and t.is_alive():
                t.join(timeout=3.0)
            self._lv_url = None
            # Stop the event long-poll thread.
            self._event_running = False
            et = self._event_thread
            self._event_thread = None
            if et is not None and et.is_alive():
                et.join(timeout=3.0)
            # Drop the HTTP session — keepalive sockets are stale after the
            # camera reboots.
            try:
                self._cmd_sess.close()
            except Exception:  # noqa: BLE001
                pass
            self._cmd_sess = requests.Session()
            self._poll_sess = self._cmd_sess
            self._available_apis = []
            self._event_state = {}

            # Best-effort: re-fetch DD.xml in case the camera came back with
            # different ports. Skip if we never had a host.
            host = None
            cam_url = self.device.services.get("camera", "")
            try:
                host = urlparse(cam_url).hostname
            except Exception:
                pass
            # Refresh services only if DD.xml actually responded — `from_host`
            # falls back to a hardcoded :8080 map when it can't fetch DD.xml,
            # and we definitely don't want to clobber a known-good services
            # dict with that. Detect via friendly_name == "(static)".
            if host:
                try:
                    new_dev = from_host(host)
                    if new_dev.services and new_dev.friendly_name != "(static)":
                        self.device = new_dev
                        log.info(
                            "reconnect: services refreshed (%s)",
                            list(new_dev.services),
                        )
                    else:
                        log.info(
                            "reconnect: DD.xml unreachable, keeping existing services"
                        )
                except Exception as e:  # noqa: BLE001
                    log.warning("reconnect: DD.xml refresh failed: %s", e)

            self._needs_reconnect = False
            # Bring everything back. start_liveview() retries internally
            # but can still fail if the camera is offline — in that case
            # leave _needs_reconnect on so the watchdog tries again.
            self._start_event_loop()
            self.start_liveview()
            if not self._lv_running:
                log.info("reconnect: liveview not yet up — will retry")
                self._needs_reconnect = True
            else:
                log.info("reconnect: done")

    # ---------- JSON-RPC ----------

    def call(
        self,
        service: str,
        method: str,
        params: Optional[list] = None,
        version: str = "1.0",
        timeout: float = 10.0,
        sess: Optional[requests.Session] = None,
    ) -> Any:
        url = self.device.url(service)
        if url is None:
            raise RuntimeError(f"unknown service: {service}")
        body = {
            "method": method,
            "params": params or [],
            "id": next(self._id_iter),
            "version": version,
        }
        s = sess or self._cmd_sess
        # Always serialize. The camera service is single-flight per device
        # regardless of which TCP connection issued the request.
        with self._cmd_lock:
            resp = s.post(url, data=json.dumps(body), timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            err = data["error"]
            code = int(err[0]) if isinstance(err, list) and err else -1
            msg = err[1] if isinstance(err, list) and len(err) > 1 else "unknown"
            raise SonyApiError(method, code, str(msg))
        return data.get("result")

    # ---------- mode + setting helpers ----------

    def set_camera_function(self, fn: str) -> None:
        """fn = 'Remote Shooting' | 'Contents Transfer'.

        After this, the set of available APIs changes. Callers that need to
        be sure the change has landed should poll get_event for
        cameraFunction == fn.
        """
        self.call("camera", "setCameraFunction", [fn])
        self._mode = fn

    def get_camera_function(self) -> Optional[str]:
        try:
            r = self.call("camera", "getCameraFunction")
        except SonyApiError:
            return None
        if isinstance(r, list) and r:
            self._mode = r[0]
        return self._mode

    def get_available_api_list(self) -> list[str]:
        r = self.call("camera", "getAvailableApiList")
        if isinstance(r, list) and r and isinstance(r[0], list):
            self._available_apis = list(r[0])
        return self._available_apis

    def set_shoot_mode(self, mode: str) -> None:
        self.call("camera", "setShootMode", [mode])
        self._shoot_mode = mode

    def get_shoot_mode(self) -> Optional[str]:
        r = self.call("camera", "getShootMode")
        if isinstance(r, list) and r:
            self._shoot_mode = r[0]
        return self._shoot_mode

    def act_take_picture(self) -> list[str]:
        """Returns the list of postview URLs (usually 1).

        Wraps the call in actHalfPressShutter / cancelHalfPressShutter —
        Sony's reference Camera Remote sample app does the same. Without
        the half-press prefix, RX100M5A in Flexible Spot focus area
        rejects actTakePicture with the misleading error `[40400, '']`
        (which the spec documents as "Already Polling" but here means
        "AF not locked, please half-press first")."""
        try:
            self.call("camera", "actHalfPressShutter")
        except SonyApiError as e:
            log.debug("actHalfPressShutter pre-shoot: %s", e)
        try:
            r = self.call("camera", "actTakePicture", timeout=15.0)
        finally:
            try:
                self.call("camera", "cancelHalfPressShutter", timeout=3.0)
            except (SonyApiError, requests.RequestException) as e:
                log.debug("cancelHalfPressShutter post-shoot: %s", e)
        if isinstance(r, list) and r and isinstance(r[0], list):
            return list(r[0])
        return []

    def act_zoom(self, direction: str, movement: str = "1shot") -> None:
        self.call("camera", "actZoom", [direction, movement])

    def start_cont_shooting(self) -> None:
        """Start continuous (burst) shooting. The camera keeps shooting
        until stop_cont_shooting() is called. Per the spec the shoot mode
        must be "still" and `setContShootingMode` should already be set on
        the camera body to a continuous variant."""
        self.call("camera", "startContShooting")

    def stop_cont_shooting(self) -> list[str]:
        """Stop a running burst. Returns the list of postview URLs the
        camera produced (one per frame in the burst)."""
        r = self.call("camera", "stopContShooting")
        if isinstance(r, list) and r and isinstance(r[0], list):
            return list(r[0])
        return []

    def start_bulb_shooting(self) -> None:
        """Open the shutter for bulb exposure. Camera must already be in
        Manual mode with shutter speed = BULB."""
        self.call("camera", "startBulbShooting")

    def stop_bulb_shooting(self) -> None:
        self.call("camera", "stopBulbShooting")

    def half_press(self, on: bool) -> None:
        method = "actHalfPressShutter" if on else "cancelHalfPressShutter"
        self.call("camera", method)

    def set_setting(self, name: str, value: Any) -> None:
        """Generic setter: maps ('iso', '400') → setIsoSpeedRate(['400'])."""
        method = _SETTING_METHODS.get(name)
        if method is None:
            raise ValueError(f"unknown setting: {name}")
        self.call("camera", method, [value])

    # ---------- event loop (state mirror) ----------

    def _start_event_loop(self) -> None:
        if self._event_running:
            return
        self._event_running = True
        self._event_thread = threading.Thread(
            target=self._event_loop, name="sodsc-event", daemon=True
        )
        self._event_thread.start()

    def _stop_event_loop(self) -> None:
        self._event_running = False
        t = self._event_thread
        self._event_thread = None
        if t is not None:
            t.join(timeout=2.0)

    def _event_loop(self) -> None:
        # Short-poll every ~2s. Long-poll (`getEvent(true)`) returns the
        # *delta* whenever state changes, which is more efficient — but it
        # keeps the camera service "busy from the camera's POV", and any
        # actTakePicture / setIso / etc. issued while it is pending gets
        # rejected with `40400 Already Polling` (RX100M5A firmware doesn't
        # multiplex). Short-polling sidesteps that at the cost of one
        # request/sec and slightly stale state. Worth it.
        backoff = 1.0
        while self._event_running:
            try:
                r = self.call(
                    "camera",
                    "getEvent",
                    [False],
                    version="1.0",
                    timeout=8.0,
                    sess=self._poll_sess,
                )
                self._absorb_event(r)
                backoff = 1.0
                time.sleep(2.0)
            except SonyApiError as e:
                log.debug("getEvent api error: %s", e)
                time.sleep(1.0)
            except requests.RequestException as e:
                log.debug("getEvent transport: %s", e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    def _absorb_event(self, result: Any) -> None:
        """getEvent returns a list of state slots, each potentially null."""
        if not isinstance(result, list):
            return
        with self._event_lock:
            for slot in result:
                if not isinstance(slot, dict):
                    continue
                t = slot.get("type")
                if not t:
                    continue
                self._event_state[t] = slot
                if t == "cameraFunction" and "currentCameraFunction" in slot:
                    self._mode = slot["currentCameraFunction"]
                elif t == "shootMode" and "currentShootMode" in slot:
                    self._shoot_mode = slot["currentShootMode"]
                elif t == "batteryInfo":
                    bs = slot.get("batteryInfo") or []
                    if bs:
                        self._battery = bs[0]
                elif t == "availableApiList":
                    apis = slot.get("names") or slot.get("availableApiList")
                    if isinstance(apis, list):
                        self._available_apis = list(apis)

    def event_state(self) -> dict:
        with self._event_lock:
            return dict(self._event_state)

    # ---------- liveview ----------

    def start_liveview(self, retries: int = 4, retry_delay: float = 1.5) -> None:
        if self._lv_running:
            return
        # Right after a mode change or a stopLiveview, the camera can return
        # error [1] "Any" / [14] "Illegal State" briefly while it re-arms.
        # Retry with backoff before giving up.
        last_err: Optional[Exception] = None
        url: Optional[str] = None
        for attempt in range(retries):
            try:
                r = self.call("camera", "startLiveview")
                if isinstance(r, list) and r and isinstance(r[0], str):
                    url = r[0]
                    break
                last_err = RuntimeError(f"unexpected result: {r!r}")
            except SonyApiError as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(retry_delay)
        if url is None:
            log.warning("startLiveview gave up after %d tries: %s", retries, last_err)
            return
        self._lv_url = url
        self._lv_running = True
        self._lv_thread = threading.Thread(
            target=self._liveview_loop, name="sodsc-liveview", daemon=True
        )
        self._lv_thread.start()
        log.info("liveview started: %s", self._lv_url)

    def supports(self, method: str) -> bool:
        """True if the method appears in the camera's available API list.

        Returns False if we haven't received an availableApiList event yet —
        callers that want to fall back to "try anyway" should prefer to just
        catch SonyApiError instead of using this guard."""
        return method in self._available_apis

    def stop_liveview(self) -> None:
        if not self._lv_running:
            return
        self._lv_running = False
        t = self._lv_thread
        self._lv_thread = None
        if t is not None:
            t.join(timeout=3.0)
        # Short timeout — if the camera is gone we don't want to block
        # shutdown for the full request timeout.
        try:
            self.call("camera", "stopLiveview", timeout=2.0)
        except (SonyApiError, requests.RequestException) as e:
            log.debug("stopLiveview: %s", e)
        self._lv_url = None
        log.info("liveview stopped")

    def _refresh_lv_url(self) -> bool:
        """Ask the camera for a fresh liveview URL. Returns True on success."""
        try:
            r = self.call("camera", "startLiveview", timeout=8.0)
        except (SonyApiError, requests.RequestException) as e:
            log.debug("startLiveview refresh failed: %s", e)
            return False
        if isinstance(r, list) and r and isinstance(r[0], str):
            self._lv_url = r[0]
            log.info("liveview: URL refreshed → %s", self._lv_url)
            return True
        return False

    def _liveview_loop(self) -> None:
        # Use a raw socket pulled out of requests for stable byte-exact reads.
        # `requests` Response.raw works but chunked/decompression handling
        # gets in the way of reading exact byte counts.
        consec_failures = 0
        while self._lv_running:
            # Make sure we have a URL. After a few failures we drop it so we
            # come back through here and refresh against the camera.
            if not self._lv_url:
                if not self._refresh_lv_url():
                    # Camera not answering — let the watchdog escalate.
                    self._needs_reconnect = True
                    return
            url = self._lv_url
            u = urlparse(url)
            host = u.hostname or ""
            port = u.port or 80
            path = u.path or "/"
            if u.query:
                path = f"{path}?{u.query}"

            sock = None
            try:
                sock = socket.create_connection((host, port), timeout=10)
                # Big rcvbuf — single frame is ~30KB but we want headroom.
                try:
                    sock.setsockopt(
                        socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024
                    )
                except OSError:
                    pass
                req = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n"
                    "Connection: keep-alive\r\n"
                    "Accept: */*\r\n"
                    "\r\n"
                ).encode("ascii")
                sock.sendall(req)
                leftover, chunked = self._consume_http_headers(sock)
                self._read_liveview_stream(sock, leftover, chunked=chunked)
                # Successful session block — reset failure tracking.
                consec_failures = 0
            except (OSError, _StreamClosed) as e:
                if not self._lv_running:
                    break
                consec_failures += 1
                backoff = min(0.5 * (2 ** (consec_failures - 1)), 5.0)
                log.info(
                    "liveview broke (#%d: %s); retry in %.1fs",
                    consec_failures, e, backoff,
                )
                time.sleep(backoff)
                # 3 same-URL failures in a row → camera probably gave us a
                # new URL after a power cycle. Force refresh on next loop.
                if consec_failures == 3:
                    log.info("liveview: forcing URL refresh")
                    self._lv_url = None
                # 6 consecutive failures even with refresh → escalate to
                # full reconnect (DD.xml + sessions). Hand off via flag —
                # don't call reconnect() from this thread, the watchdog
                # owns that path so we don't self-join.
                if consec_failures >= 6:
                    log.warning(
                        "liveview: %d consecutive failures — requesting full reconnect",
                        consec_failures,
                    )
                    self._needs_reconnect = True
                    return
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

    @staticmethod
    def _consume_http_headers(sock: socket.socket) -> tuple[bytes, bool]:
        """Read until the blank line that ends HTTP response headers.

        Returns (leftover, chunked) where leftover is bytes already past
        the terminator (the start of the body), and chunked is True iff
        Transfer-Encoding: chunked is set. RX100M5A sends chunked even when
        we ask for HTTP/1.0; we have to unwrap it inline.
        """
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise _StreamClosed("eof before headers")
            buf += chunk
            if len(buf) > 16 * 1024:
                raise _StreamClosed("header runaway")
        end = buf.index(b"\r\n\r\n") + 4
        head = buf[:end]
        chunked = b"chunked" in head.lower()
        return buf[end:], chunked

    def _read_liveview_stream(
        self, sock: socket.socket, prebuf: bytes = b"", chunked: bool = False
    ) -> None:
        # When the body is sent with Transfer-Encoding: chunked (RX100M5A
        # does this), peel off chunk-size lines + trailing CRLF inline.
        if chunked:
            body_buf = bytearray(prebuf)
            chunk_remaining = 0
            consumed_trailing_crlf = True

            def fill_to(min_bytes: int) -> None:
                while len(body_buf) < min_bytes:
                    if not self._lv_running:
                        raise _StreamClosed("stop requested")
                    got = sock.recv(65536)
                    if not got:
                        raise _StreamClosed("eof mid-frame")
                    body_buf.extend(got)

            def read_exact(n: int) -> bytes:
                nonlocal chunk_remaining, consumed_trailing_crlf
                out = bytearray()
                while len(out) < n:
                    if chunk_remaining == 0:
                        if not consumed_trailing_crlf:
                            fill_to(2)
                            del body_buf[:2]
                            consumed_trailing_crlf = True
                        # Read the chunk-size line (hex digits + optional ext + CRLF)
                        while b"\r\n" not in body_buf:
                            fill_to(len(body_buf) + 1)
                        nl = body_buf.index(b"\r\n")
                        size_str = bytes(body_buf[:nl]).split(b";", 1)[0].strip()
                        del body_buf[: nl + 2]
                        try:
                            chunk_remaining = int(size_str, 16)
                        except ValueError:
                            raise _StreamClosed(f"bad chunk size: {size_str!r}")
                        if chunk_remaining == 0:
                            raise _StreamClosed("end of chunked body")
                        consumed_trailing_crlf = False
                    if not body_buf:
                        fill_to(1)
                    take = min(n - len(out), chunk_remaining, len(body_buf))
                    out.extend(body_buf[:take])
                    del body_buf[:take]
                    chunk_remaining -= take
                return bytes(out)
        else:
            local_buf = prebuf

            def read_exact(n: int) -> bytes:
                nonlocal local_buf
                if len(local_buf) >= n:
                    out, local_buf = local_buf[:n], local_buf[n:]
                    return out
                chunks = [local_buf]
                need = n - len(local_buf)
                local_buf = b""
                while need > 0:
                    if not self._lv_running:
                        raise _StreamClosed("stop requested")
                    got = sock.recv(min(65536, need))
                    if not got:
                        raise _StreamClosed("eof mid-frame")
                    chunks.append(got)
                    need -= len(got)
                data = b"".join(chunks)
                if len(data) > n:
                    local_buf = data[n:]
                    data = data[:n]
                return data

        while self._lv_running:
            head = read_exact(8)
            if head[0] != 0xFF:
                # Resync: scan forward for a 0xFF byte. This shouldn't happen
                # in a well-behaved stream but guards against corruption.
                log.debug("liveview: bad start byte %02x — resyncing", head[0])
                idx = head.find(b"\xff", 1)
                if idx < 0:
                    continue
                head = head[idx:] + read_exact(idx)
                if head[0] != 0xFF:
                    continue
            payload_type = head[1]
            seq = int.from_bytes(head[2:4], "big")
            timestamp_ms = int.from_bytes(head[4:8], "big")

            pheader = read_exact(128)
            if payload_type == 0x01:
                jpeg_size = int.from_bytes(pheader[4:7], "big")
                padding_size = pheader[7]
                if jpeg_size <= 0 or jpeg_size > 4 * 1024 * 1024:
                    log.warning("liveview: implausible jpeg_size=%d, dropping", jpeg_size)
                    raise _StreamClosed("bad jpeg_size")
                jpeg = read_exact(jpeg_size)
                if padding_size:
                    read_exact(padding_size)
                self._publish(_Frame(seq, timestamp_ms, jpeg))
            elif payload_type == 0x02:
                # Frame info packet — skip frame_count*frame_size bytes.
                # pheader[8:10] = frame_count BE u16, pheader[10:12] = frame_size BE u16
                # (per the public spec; values are 0 when frame info is disabled.)
                jpeg_size = int.from_bytes(pheader[4:7], "big")
                padding_size = pheader[7]
                if jpeg_size:
                    read_exact(jpeg_size)
                if padding_size:
                    read_exact(padding_size)
            else:
                log.debug("liveview: unknown payload type %02x", payload_type)
                # Without payload-length info we can't safely skip; force
                # reconnect.
                raise _StreamClosed("unknown payload type")

    # ---------- subscribers / latest ----------

    def _publish(self, frame: _Frame) -> None:
        with self._latest_lock:
            self._latest_frame = frame.jpeg
            self._latest_seq = frame.seq
        self._stats.frames_seen += 1
        self._stats.last_frame_at = time.time()
        self._stats.last_seq = frame.seq

        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(frame.jpeg)
            except queue.Full:
                # Drop oldest — viewer can't keep up, prefer freshness over
                # delay buildup.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(frame.jpeg)
                except queue.Full:
                    pass

    def subscribe(self, maxsize: int = 4) -> queue.Queue:
        q: queue.Queue[bytes] = queue.Queue(maxsize=maxsize)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def latest_frame(self) -> tuple[Optional[bytes], int]:
        with self._latest_lock:
            return self._latest_frame, self._latest_seq

    # ---------- watchdog (auto reconnect) ----------

    def _start_watchdog(self) -> None:
        if self._wd_running:
            return
        self._wd_running = True
        self._wd_thread = threading.Thread(
            target=self._watchdog_loop, name="sodsc-watchdog", daemon=True
        )
        self._wd_thread.start()

    def _stop_watchdog(self) -> None:
        self._wd_running = False
        t = self._wd_thread
        self._wd_thread = None
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def _watchdog_loop(self) -> None:
        """Detect stuck liveview / dead camera and trigger a full reconnect.

        Two triggers:
        - The liveview reader thread set _needs_reconnect (gave up after N
          consecutive stream failures, or saw the URL go bad).
        - The stream is supposedly running but no frames have arrived for
          longer than _idle_reconnect_threshold seconds — typical signature
          of a camera power-cycle that we missed."""
        backoff = 2.0
        while self._wd_running:
            time.sleep(2.0)

            # Two reconnect signals are equivalent — coalesce here.
            stalled = False
            if self._lv_running and self._stats.last_frame_at:
                idle = time.time() - self._stats.last_frame_at
                if idle > self._idle_reconnect_threshold:
                    log.warning(
                        "watchdog: %.0fs without a frame — assuming dead camera",
                        idle,
                    )
                    stalled = True

            if not (self._needs_reconnect or stalled):
                backoff = 2.0
                continue

            try:
                self.reconnect()
            except Exception as e:  # noqa: BLE001
                log.error("watchdog reconnect raised: %s", e)

            if self._lv_running:
                backoff = 2.0
            else:
                # Camera still not reachable. Sleep with exponential
                # backoff (capped at 30s) before the next attempt, so we
                # don't spam reconnects every 2 seconds against a dead
                # endpoint.
                log.info("watchdog: liveview not up, retry in %.0fs", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # ---------- introspection ----------

    def stats(self) -> dict:
        now = time.time()
        idle = (
            (now - self._stats.last_frame_at) if self._stats.last_frame_at else None
        )
        return {
            "device": {
                "friendly_name": self.device.friendly_name,
                "model_name": self.device.model_name,
                "services": self.device.services,
            },
            "frames_seen": self._stats.frames_seen,
            "frames_dropped": self._stats.frames_dropped,
            "subscribers": len(self._subscribers),
            "running": self._lv_running,
            "idle_seconds": idle,
            "seq": self._stats.last_seq,
            "mode": self._mode,
            "shoot_mode": self._shoot_mode,
            "battery": self._battery,
            "available_apis": list(self._available_apis),
            "liveview_url": self._lv_url,
            "needs_reconnect": self._needs_reconnect,
            "watchdog_running": self._wd_running,
        }


class _StreamClosed(Exception):
    pass


# Map of friendly setting names → Camera Remote API method.
_SETTING_METHODS = {
    "iso": "setIsoSpeedRate",
    "shutter": "setShutterSpeed",
    "fnumber": "setFNumber",
    "ev": "setExposureCompensation",
    "wb": "setWhiteBalance",
    "focus": "setFocusMode",
    "still_size": "setStillSize",
    "flash": "setFlashMode",
    "self_timer": "setSelfTimer",
    "shoot_mode": "setShootMode",
    "beep": "setBeepMode",
}
