"""UPnP ContentDirectory client for "Send to Smartphone" mode.

When the camera body switches the in-camera app from "Smart Remote
Control" to "スマートフォンに送る", the JSON-RPC Camera Remote API
disappears entirely and is replaced by a **standard UPnP/DLNA
MediaServer**. The avContent JSON-RPC service is NOT exposed in this
mode — instead we have to talk SOAP to a ContentDirectory:1 service.

This module is the SOAP path. Item structure (RX100M5A):

    Root (id=0)
      └─ PhotoRoot
           └─ Date  (id like 02_00_...)
                └─ <yyyy-m-d>  (id like 03_01_...)
                     └─ DSC*****.ARW  (id like 04_02_...)
                          ├─ res JPEG_TN  (thumbnail)
                          ├─ res JPEG_SM  (small)
                          └─ res JPEG_LRG (large preview)

The original ARW (RAW) file is **not** retrievable via DLNA — Sony's
implementation only exposes converted JPEGs at three sizes. For
full-resolution originals the user has to use Camera Remote API
avContent (which RX100M5A does not expose) or pull the SD card.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Iterator, Optional

import requests

log = logging.getLogger(__name__)

NS_CONTENT_DIR = "urn:schemas-upnp-org:service:ContentDirectory:1"
SOAP_ACTION = f'"{NS_CONTENT_DIR}#Browse"'

# The DLNA convention encodes res size in the URL prefix.
SIZE_PREFIXES = {"TN_": "thumbnail", "SM_": "small", "LRG_": "large"}


def browse(
    control_url: str,
    object_id: str,
    *,
    flag: str = "BrowseDirectChildren",
    start: int = 0,
    count: int = 50,
    timeout: float = 10.0,
) -> Optional[str]:
    """Issue a SOAP Browse and return the inner DIDL-Lite XML body
    (entity-decoded). Returns None on transport / SOAP error."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:Browse xmlns:u="{NS_CONTENT_DIR}">'
        f"<ObjectID>{object_id}</ObjectID>"
        f"<BrowseFlag>{flag}</BrowseFlag>"
        "<Filter>*</Filter>"
        f"<StartingIndex>{start}</StartingIndex>"
        f"<RequestedCount>{count}</RequestedCount>"
        "<SortCriteria></SortCriteria>"
        "</u:Browse></s:Body></s:Envelope>"
    )
    try:
        r = requests.post(
            control_url,
            data=body,
            timeout=timeout,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": SOAP_ACTION,
            },
        )
    except requests.RequestException as e:
        log.warning("UPnP Browse %s failed: %s", object_id, e)
        return None
    if r.status_code != 200:
        log.warning("UPnP Browse %s status %d", object_id, r.status_code)
        return None
    m = re.search(r"<Result>(.*?)</Result>", r.text, re.DOTALL)
    if not m:
        return None
    return html.unescape(m.group(1))


_RE_CONTAINER = re.compile(
    r'<container\s+id="([^"]+)"[^>]*childCount="(\d+)"[^>]*>(.*?)</container>',
    re.DOTALL,
)
_RE_ITEM = re.compile(r'<item\s+id="([^"]+)"[^>]*>(.*?)</item>', re.DOTALL)
_RE_TITLE = re.compile(r"<dc:title>([^<]+)</dc:title>")
_RE_DATE = re.compile(r"<dc:date>([^<]+)</dc:date>")
_RE_RES = re.compile(r'<res\s+([^>]+)>([^<]+)</res>')
_RE_PROTO = re.compile(r'protocolInfo="([^"]+)"')
_RE_SIZE = re.compile(r'size="(\d+)"')
_RE_RESL = re.compile(r'resolution="([^"]+)"')
_RE_KIND = re.compile(r"<av:contentType>([^<]+)</av:contentType>")


def parse_item(it_xml: str) -> dict:
    """Parse a single DIDL-Lite item body (between <item ...> and </item>)."""
    title_m = _RE_TITLE.search(it_xml)
    date_m = _RE_DATE.search(it_xml)
    kind_m = _RE_KIND.search(it_xml)
    out = {
        "title": title_m.group(1) if title_m else None,
        "createdTime": date_m.group(1) if date_m else None,
        "contentKind": kind_m.group(1) if kind_m else None,
        "thumbnailUrl": None,
        "smallUrl": None,
        "largeUrl": None,
        "originalUrl": None,
        "originalFilename": None,
    }
    for r in _RE_RES.finditer(it_xml):
        attrs, url = r.group(1), r.group(2)
        proto_m = _RE_PROTO.search(attrs)
        proto = proto_m.group(1) if proto_m else ""
        # Bucket by DLNA profile name in protocolInfo
        if "JPEG_TN" in proto:
            out["thumbnailUrl"] = url
        elif "JPEG_SM" in proto:
            out["smallUrl"] = url
        elif "JPEG_LRG" in proto:
            out["largeUrl"] = url
            out["originalUrl"] = url  # closest we can get via DLNA
            # Strip the "LRG_" prefix to get the original-ish filename
            tail = url.rsplit("/", 1)[-1].split("?", 1)[0]
            if tail.startswith("LRG_"):
                out["originalFilename"] = tail[4:]
            else:
                out["originalFilename"] = tail
    return out


def walk_photos(control_url: str, max_items: int = 500) -> Iterator[dict]:
    """Yield image items by walking the photo tree.

    Walks Root → PhotoRoot → Date → date-folders → items. Items are
    yielded in the order the camera returns them (RX100M5A: ascending
    by createdTime). The server **does not** re-sort — the response
    carries `createdTime` per item, and clients (the bundled UI, MCP
    consumers) can sort or filter as they wish. Keeping the server
    order-agnostic also keeps pagination cheap: we don't have to walk
    the whole tree before yielding the first item."""
    # 1) Root → PhotoRoot
    root = browse(control_url, "0", count=10) or ""
    photo_root_id = None
    for m in _RE_CONTAINER.finditer(root):
        if "PhotoRoot" in m.group(3):
            photo_root_id = m.group(1)
            break
    if not photo_root_id:
        return

    # 2) PhotoRoot → Date container
    photo_root = browse(control_url, photo_root_id, count=10) or ""
    date_root_id = None
    for m in _RE_CONTAINER.finditer(photo_root):
        if "<dc:title>Date</dc:title>" in m.group(3):
            date_root_id = m.group(1)
            break
    if not date_root_id:
        return

    # 3) Date container → date-folders → items.
    yielded = 0
    start = 0
    while yielded < max_items:
        page = browse(control_url, date_root_id, start=start, count=50) or ""
        date_folders = list(_RE_CONTAINER.finditer(page))
        if not date_folders:
            break
        for df in date_folders:
            df_id = df.group(1)
            sub_start = 0
            while yielded < max_items:
                sub_page = (
                    browse(control_url, df_id, start=sub_start, count=50) or ""
                )
                items = list(_RE_ITEM.finditer(sub_page))
                if not items:
                    break
                for it in items:
                    parsed = parse_item(it.group(2))
                    parsed["uri"] = it.group(1)
                    yield parsed
                    yielded += 1
                    if yielded >= max_items:
                        return
                sub_start += len(items)
                if len(items) < 50:
                    break
        start += len(date_folders)
        if len(date_folders) < 50:
            break
