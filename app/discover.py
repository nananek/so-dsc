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
    """Resolved endpoint for a Sony device.

    `services` holds Camera Remote API JSON-RPC services (camera /
    avContent / system / accessControl etc.) when the in-camera app
    advertises them.

    `content_directory_url` is the SOAP control URL for the standard
    UPnP ContentDirectory service. RX100M5A exposes this **only when
    the in-camera app is "Send to Smartphone"** — at the same time the
    JSON-RPC services disappear. Other models may expose both at once.
    """

    friendly_name: str
    model_name: str
    udn: str
    services: dict[str, str]  # {"camera": "http://.../sony/camera", ...}
    content_directory_url: Optional[str] = None
    # IP/host of the camera. Kept separately because services may be
    # empty (camera offline at boot, mode change with no overlap) and
    # refresh paths still need to know who to ask.
    host: Optional[str] = None

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

    Both DD.xml flavors are handled:

    * "SonyRemoteCamera" (Smart Remote Control mode): JSON-RPC services
    * "SonyDigitalMediaServer" (Send-to-Smartphone mode): standard UPnP
      ContentDirectory at /upnp/control/ContentDirectory
    """
    location = f"http://{host}:{dd_port}/dd.xml"
    dev = _fetch_dd(location)
    if dev is not None:
        return dev
    log.warning(
        "DD.xml fetch from %s failed; returning empty device — "
        "watchdog will retry once the camera is reachable",
        location,
    )
    return SonyDevice(
        friendly_name="(static)",
        model_name="(unknown)",
        udn="",
        services={},
        host=host,
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

    # Standard UPnP ContentDirectory — present in "Send to Smartphone" mode
    # in place of the JSON-RPC services. We extract the controlURL and
    # join it with the device base so it's an absolute URL the SOAP
    # client can post to.
    content_dir_url: Optional[str] = None
    base_url = location.rsplit("/", 1)[0]
    for svc in root.iter():
        st = svc.tag.rsplit("}", 1)[-1]
        if st != "service":
            continue
        type_text = ""
        ctrl_text = ""
        for child in svc:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "serviceType":
                type_text = (child.text or "").strip()
            elif tag == "controlURL":
                ctrl_text = (child.text or "").strip()
        if "ContentDirectory" in type_text and ctrl_text:
            if ctrl_text.startswith("http://") or ctrl_text.startswith("https://"):
                content_dir_url = ctrl_text
            elif ctrl_text.startswith("/"):
                # ctrl is path-only — join with the dd.xml host:port
                from urllib.parse import urlparse as _u
                u = _u(location)
                content_dir_url = f"{u.scheme}://{u.netloc}{ctrl_text}"
            else:
                content_dir_url = f"{base_url}/{ctrl_text}"
            break

    if not services and not content_dir_url:
        log.warning("DD.xml has no Sony service entries and no ContentDirectory")
        return None

    from urllib.parse import urlparse as _u
    parsed_loc = _u(location)
    return SonyDevice(
        friendly_name=_text("friendlyName"),
        model_name=_text("modelName"),
        udn=_text("UDN"),
        services=services,
        content_directory_url=content_dir_url,
        host=parsed_loc.hostname,
    )
