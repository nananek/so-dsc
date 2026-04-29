"""SSDP discovery + UPnP DD.xml parsing for Sony Camera Remote API.

Wire format and method list: ../docs/protocol.md
"""
from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.request import urlopen
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_ST = "urn:schemas-sony-com:service:ScalarWebAPI:1"

NS_AV = "urn:schemas-sony-com:av"


@dataclass
class SonyDevice:
    """Resolved endpoint for a Sony Camera Remote API device."""

    friendly_name: str
    model_name: str
    udn: str
    services: dict[str, str]  # {"camera": "http://.../sony/camera", ...}

    def url(self, service: str) -> Optional[str]:
        return self.services.get(service)


def discover(timeout: float = 3.0, retries: int = 2) -> Optional[SonyDevice]:
    """Send SSDP M-SEARCH and resolve the first responding Sony device.

    Returns None on timeout. The 1-client AP means we will only ever see one
    device, but we still loop the recv to drain duplicates from retries.
    """
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        f"ST: {SSDP_ST}\r\n"
        "\r\n"
    ).encode("ascii")

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.bind(("0.0.0.0", 0))
    s.settimeout(timeout)

    location: Optional[str] = None
    try:
        for _ in range(retries):
            try:
                s.sendto(msg, (SSDP_ADDR, SSDP_PORT))
            except OSError as e:
                log.warning("SSDP send failed: %s", e)
                return None
            try:
                while True:
                    data, _addr = s.recvfrom(4096)
                    loc = _parse_location(data)
                    if loc:
                        location = loc
                        break
            except socket.timeout:
                pass
            if location:
                break
    finally:
        s.close()

    if not location:
        log.info("SSDP discovery: no response")
        return None
    return _fetch_dd(location)


def from_host(host: str, dd_port: int = 64321) -> SonyDevice:
    """Build a SonyDevice for a known host. Tries to fetch /dd.xml so we
    pick up the camera's actual API port (RX100M5A uses 10000, others use
    8080 — varies by model and even by firmware), then falls back to a
    hardcoded service map if the camera hasn't booted its UPnP server yet.
    """
    location = f"http://{host}:{dd_port}/dd.xml"
    dev = _fetch_dd(location)
    if dev is not None:
        return dev
    log.warning("DD.xml fetch from %s failed; using fallback service map", location)
    base = f"http://{host}:8080/sony"
    return SonyDevice(
        friendly_name="(static)",
        model_name="(unknown)",
        udn="",
        services={
            "camera": f"{base}/camera",
            "system": f"{base}/system",
            "avContent": f"{base}/avContent",
            "guide": f"{base}/guide",
        },
    )


_LOC_RE = re.compile(rb"^LOCATION:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


def _parse_location(data: bytes) -> Optional[str]:
    m = _LOC_RE.search(data)
    if not m:
        return None
    return m.group(1).decode("ascii", "replace")


def _fetch_dd(location: str) -> Optional[SonyDevice]:
    try:
        with urlopen(location, timeout=5) as resp:  # noqa: S310 (LAN device)
            xml = resp.read()
    except OSError as e:
        log.warning("DD.xml fetch failed (%s): %s", location, e)
        return None

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        log.warning("DD.xml parse failed: %s", e)
        return None

    # Default UPnP namespace varies; pull friendly fields via local-name match.
    def _text(tag: str) -> str:
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] == tag and el.text:
                return el.text.strip()
        return ""

    services: dict[str, str] = {}
    for svc in root.iter(f"{{{NS_AV}}}X_ScalarWebAPI_Service"):
        kind = svc.findtext(f"{{{NS_AV}}}X_ScalarWebAPI_ServiceType") or ""
        action = svc.findtext(f"{{{NS_AV}}}X_ScalarWebAPI_ActionList_URL") or ""
        if kind and action:
            services[kind.strip()] = f"{action.strip().rstrip('/')}/{kind.strip()}"

    if not services:
        log.warning("DD.xml has no Sony service entries")
        return None

    return SonyDevice(
        friendly_name=_text("friendlyName"),
        model_name=_text("modelName"),
        udn=_text("UDN"),
        services=services,
    )
