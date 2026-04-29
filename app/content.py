"""avContent service wrapper: browse and download camera-stored media.

Wire format and method list: ../docs/protocol.md
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests

from .sony import SonyApiError, SonyClient

log = logging.getLogger(__name__)


class ModeSwitchUnsupported(RuntimeError):
    """The camera/firmware does not expose setCameraFunction; mode switch
    has to happen on the camera body."""


# RX100M5A typically has one storage source: storage:memoryCard1. We probe
# every time rather than hardcoding so a different model works as-is.
DEFAULT_VIEW = "date"
DEFAULT_SORT = "descending"
PAGE_SIZE = 100


class ContentBrowser:
    """Manages the camera mode toggle and fetches/streams content.

    The Camera Remote API gates avContent behind setCameraFunction(
    "Contents Transfer"), and ditto in reverse for live shooting. This
    class owns that mode dance so callers can think in terms of "I want
    to list" / "I want to shoot"."""

    def __init__(self, client: SonyClient, download_dir: Path):
        self.client = client
        self.download_dir = download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._mode_lock = threading.Lock()

    # ---------- mode management ----------

    def enter_transfer_mode(self) -> None:
        """Switch the camera to Contents Transfer and wait for it to land.

        Raises ModeSwitchUnsupported on cameras (e.g. RX100M5A) whose Smart
        Remote Control app does not expose setCameraFunction — those
        require the user to switch in-camera apps on the body."""
        if not self.client.supports("setCameraFunction"):
            raise ModeSwitchUnsupported(
                "this camera/firmware doesn't expose setCameraFunction. "
                "Switch the in-camera app to 'Send to Smartphone' on the "
                "body to use the content browser."
            )
        with self._mode_lock:
            try:
                self.client.set_camera_function("Contents Transfer")
            except SonyApiError as e:
                # 'Illegal State' usually means we are already in this mode.
                log.debug("setCameraFunction(Contents Transfer): %s", e)
            self._wait_for_mode("Contents Transfer", timeout=10)

    def exit_transfer_mode(self) -> None:
        """Switch back to Remote Shooting and re-arm the liveview."""
        if not self.client.supports("setCameraFunction"):
            raise ModeSwitchUnsupported(
                "this camera/firmware doesn't expose setCameraFunction"
            )
        with self._mode_lock:
            # Stop any running liveview first — entering Remote Shooting
            # while liveview is half-open from the previous session causes
            # the camera to refuse the new startLiveview.
            self.client.stop_liveview()
            try:
                self.client.set_camera_function("Remote Shooting")
            except SonyApiError as e:
                log.debug("setCameraFunction(Remote Shooting): %s", e)
            self._wait_for_mode("Remote Shooting", timeout=10)
            self.client.start_liveview()

    def _wait_for_mode(self, target: str, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                cur = self.client.get_camera_function()
            except SonyApiError:
                cur = None
            if cur == target:
                return
            time.sleep(0.4)
        log.warning("camera did not reach mode=%s within %.1fs", target, timeout)

    # ---------- listing ----------

    def list(
        self,
        offset: int = 0,
        count: int = PAGE_SIZE,
        view: str = DEFAULT_VIEW,
        sort: str = DEFAULT_SORT,
    ) -> dict:
        # Try a soft mode switch only when the camera advertises it. Bodies
        # that don't (RX100M5A in Smart Remote) will need a manual app
        # switch on the body — we don't tear down the liveview for nothing.
        if self.client.supports("setCameraFunction"):
            try:
                self.enter_transfer_mode()
            except ModeSwitchUnsupported:
                pass

        if "avContent" not in self.client.device.services:
            return {
                "source": None,
                "total": 0,
                "items": [],
                "note": "avContent service not advertised — switch the camera "
                        "app to 'Send to Smartphone' on the body",
            }

        sources = self._sources()
        if not sources:
            return {
                "source": None,
                "total": 0,
                "items": [],
                "note": "no storage source — is the camera in Send to "
                        "Smartphone mode?",
            }
        source = sources[0]

        try:
            r = self.client.call(
                "avContent",
                "getContentCount",
                [{"source": source, "view": view}],
                version="1.2",
            )
            total = 0
            if isinstance(r, list) and r and isinstance(r[0], dict):
                total = int(r[0].get("count", 0))
        except SonyApiError as e:
            log.warning("getContentCount failed: %s", e)
            total = 0

        items: list[dict] = []
        if total > 0:
            try:
                r = self.client.call(
                    "avContent",
                    "getContentList",
                    [
                        {
                            "uri": source,
                            "stIdx": offset,
                            "cnt": count,
                            "view": view,
                            "sort": sort,
                        }
                    ],
                    version="1.3",
                )
                if isinstance(r, list) and r and isinstance(r[0], list):
                    items = [_normalize_item(it) for it in r[0] if isinstance(it, dict)]
            except SonyApiError as e:
                # Some firmwares prefer "source" key instead of "uri", and
                # version 1.2 instead of 1.3. Retry once with that shape.
                log.debug("getContentList v1.3 failed (%s); retrying v1.2", e)
                try:
                    r = self.client.call(
                        "avContent",
                        "getContentList",
                        [
                            {
                                "source": source,
                                "stIdx": offset,
                                "cnt": count,
                                "view": view,
                                "sort": sort,
                            }
                        ],
                        version="1.2",
                    )
                    if isinstance(r, list) and r and isinstance(r[0], list):
                        items = [_normalize_item(it) for it in r[0] if isinstance(it, dict)]
                except SonyApiError as e2:
                    log.warning("getContentList retry failed: %s", e2)

        return {"source": source, "total": total, "items": items}

    def _sources(self) -> list[str]:
        try:
            schemes = self.client.call("avContent", "getSchemeList")
        except SonyApiError as e:
            log.warning("getSchemeList: %s", e)
            return []
        scheme = "storage"
        if isinstance(schemes, list) and schemes and isinstance(schemes[0], list):
            entries = schemes[0]
            for e in entries:
                if isinstance(e, dict) and e.get("scheme"):
                    scheme = e["scheme"]
                    break
        try:
            r = self.client.call("avContent", "getSourceList", [{"scheme": scheme}])
        except SonyApiError as e:
            log.warning("getSourceList: %s", e)
            return []
        if not (isinstance(r, list) and r and isinstance(r[0], list)):
            return []
        return [
            item["source"]
            for item in r[0]
            if isinstance(item, dict) and item.get("source")
        ]

    # ---------- download ----------

    def download(self, url: str, name: Optional[str] = None) -> Path:
        """Stream-download a content URL into download_dir. Returns the
        local path."""
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(f"refusing non-http URL: {url!r}")
        if name is None:
            name = url.rsplit("/", 1)[-1].split("?", 1)[0] or f"download_{int(time.time())}"
        # Reject path traversal in the name.
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"unsafe filename: {name!r}")
        dest = self.download_dir / name
        # Don't overwrite — disambiguate by a counter.
        if dest.exists():
            stem, dot, ext = name.partition(".")
            for i in range(1, 1000):
                cand = self.download_dir / f"{stem}_{i:03d}{dot}{ext}"
                if not cand.exists():
                    dest = cand
                    break
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
        log.info("downloaded %s → %s (%d bytes)", url, dest, dest.stat().st_size)
        return dest

    def list_downloaded(self) -> list[dict]:
        items: list[dict] = []
        for p in sorted(self.download_dir.iterdir(), reverse=True):
            if not p.is_file():
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            items.append(
                {"name": p.name, "size_bytes": stat.st_size, "modified": stat.st_mtime}
            )
        return items

    def downloaded_path(self, name: str) -> Optional[Path]:
        if "/" in name or "\\" in name or ".." in name:
            return None
        p = self.download_dir / name
        if not p.is_file():
            return None
        return p


def _normalize_item(it: dict) -> dict:
    """Flatten the relevant fields out of a getContentList entry.

    Spec is sprawling; UI only cares about a handful of fields."""
    content = it.get("content") or {}
    originals = content.get("original") or []
    orig_url = ""
    orig_filename = ""
    if originals and isinstance(originals[0], dict):
        orig_url = originals[0].get("url") or ""
        orig_filename = originals[0].get("fileName") or ""
    return {
        "uri": it.get("uri"),
        "title": it.get("title"),
        "createdTime": it.get("createdTime"),
        "contentKind": it.get("contentKind"),
        "thumbnailUrl": content.get("thumbnailUrl"),
        "smallUrl": content.get("smallUrl"),
        "largeUrl": content.get("largeUrl"),
        "originalUrl": orig_url,
        "originalFilename": orig_filename,
    }
