#!/usr/bin/env python3
"""Dependency-free single-frame liveview grabber.

Smoke test for the Camera Remote API path. Uses only stdlib (urllib + socket).

Usage:
    # Connect laptop to the camera Wi-Fi first; put camera in Smart Remote.
    python3 scripts/grab_frame.py            # writes frame.jpg
    python3 scripts/grab_frame.py out.jpg
    SODSC_HOST=192.168.122.1 python3 scripts/grab_frame.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def jsonrpc(url: str, method: str, params=None, version: str = "1.0") -> dict:
    body = json.dumps({"method": method, "params": params or [], "id": 1, "version": version}).encode()
    req = Request(url, data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=10) as r:  # noqa: S310 (LAN device)
        return json.loads(r.read())


def _fetch_api_port(host: str) -> int | None:
    """Pull the camera service port out of /dd.xml (no XML parser needed)."""
    try:
        with urlopen(f"http://{host}:64321/dd.xml", timeout=4) as r:  # noqa: S310
            xml = r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"dd.xml fetch failed: {e}")
        return None
    # Cheap extraction: find an ActionList_URL whose service is "camera".
    import re
    for m in re.finditer(
        r"<av:X_ScalarWebAPI_ServiceType>([^<]+)</av:X_ScalarWebAPI_ServiceType>"
        r"\s*<av:X_ScalarWebAPI_ActionList_URL>([^<]+)</av:X_ScalarWebAPI_ActionList_URL>",
        xml,
    ):
        if m.group(1).strip() == "camera":
            url = m.group(2).strip()
            mp = re.search(r":(\d+)/", url)
            if mp:
                return int(mp.group(1))
    return None


def grab(host: str, out_path: str) -> None:
    # Camera API port varies by model and firmware (RX100M5A = 10000,
    # other DSC = 8080). DD.xml on :64321 is authoritative.
    api_port = _fetch_api_port(host) or 8080
    base = f"http://{host}:{api_port}/sony"
    cam = f"{base}/camera"

    # Best-effort. Some firmwares need startRecMode, others don't.
    for m in ("startRecMode",):
        try:
            r = jsonrpc(cam, m)
            print(f"{m}: {r}")
        except Exception as e:
            print(f"{m} (ignored): {e}")

    r = jsonrpc(cam, "startLiveview")
    if "error" in r:
        raise SystemExit(f"startLiveview failed: {r['error']}")
    lv_url = r["result"][0]
    print(f"liveview URL: {lv_url}")

    u = urlparse(lv_url)
    s = socket.create_connection((u.hostname, u.port or 80), timeout=10)
    path = u.path + (f"?{u.query}" if u.query else "")
    s.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nConnection: close\r\n\r\n".encode()
    )

    # Drain HTTP response headers and detect chunked encoding (RX100M5A
    # sends Transfer-Encoding: chunked even for HTTP/1.0).
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise SystemExit("eof before headers")
        buf += chunk
    head_end = buf.index(b"\r\n\r\n") + 4
    chunked = b"chunked" in buf[:head_end].lower()
    body = bytearray(buf[head_end:])
    state = {"remaining": 0, "consumed_crlf": True}

    def fill_to(n: int) -> None:
        while len(body) < n:
            got = s.recv(65536)
            if not got:
                raise SystemExit("eof mid-frame")
            body.extend(got)

    def need(n: int) -> bytes:
        if not chunked:
            fill_to(n)
            out, body[:] = bytes(body[:n]), body[n:]
            return out
        out = bytearray()
        while len(out) < n:
            if state["remaining"] == 0:
                if not state["consumed_crlf"]:
                    fill_to(2); del body[:2]; state["consumed_crlf"] = True
                while b"\r\n" not in body:
                    fill_to(len(body) + 1)
                nl = body.index(b"\r\n")
                size_str = bytes(body[:nl]).split(b";", 1)[0].strip()
                del body[: nl + 2]
                size = int(size_str, 16)
                if size == 0:
                    raise SystemExit("end of chunked body")
                state["remaining"] = size
                state["consumed_crlf"] = False
            if not body:
                fill_to(1)
            take = min(n - len(out), state["remaining"], len(body))
            out.extend(body[:take]); del body[:take]
            state["remaining"] -= take
        return bytes(out)

    # Skip frames until we get one of payload_type==0x01 (a real liveview frame
    # — frame info packets can come first).
    while True:
        head = need(8)
        if head[0] != 0xFF:
            raise SystemExit(f"bad start byte: {head[0]:02x}")
        ptype = head[1]
        pheader = need(128)
        jpeg_size = int.from_bytes(pheader[4:7], "big")
        padding = pheader[7]
        payload = need(jpeg_size)
        if padding:
            need(padding)
        if ptype == 0x01:
            with open(out_path, "wb") as f:
                f.write(payload)
            print(f"wrote {out_path} ({len(payload)} bytes)")
            break

    try:
        s.close()
    except OSError:
        pass

    try:
        jsonrpc(cam, "stopLiveview")
    except Exception:
        pass


if __name__ == "__main__":
    host = os.environ.get("SODSC_HOST", "192.168.122.1")
    out = sys.argv[1] if len(sys.argv) > 1 else "frame.jpg"
    grab(host, out)
