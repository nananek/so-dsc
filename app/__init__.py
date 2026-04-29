"""so-dsc: Sony Camera Remote API Flask viewer."""
from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path

from flask import Flask

from .content import ContentBrowser
from .discover import discover, from_host
from .recording import RecordingManager
from .sony import SonyClient
from .views import bp


def create_app() -> Flask:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = Flask(__name__)

    host = os.environ.get("SODSC_HOST")
    if host:
        device = from_host(host, int(os.environ.get("SODSC_DD_PORT", "64321")))
        app.logger.info(
            "using static host %s → services=%s", host, list(device.services)
        )
    else:
        device = discover()
        if device is None:
            # Fall back to the default RX series IP and let the user fix the
            # network if the call fails. Crashing on startup makes dev painful.
            app.logger.warning(
                "SSDP discovery failed; falling back to 192.168.122.1"
            )
            device = from_host("192.168.122.1")
        else:
            app.logger.info(
                "discovered: %s (%s) services=%s",
                device.friendly_name,
                device.model_name,
                list(device.services),
            )

    rec_dir = Path(os.environ.get("SODSC_REC_DIR", "recordings")).resolve()
    dl_dir = Path(os.environ.get("SODSC_DL_DIR", "downloads")).resolve()

    client = SonyClient(device)
    client.start()
    atexit.register(client.stop)

    app.config["SONY_CLIENT"] = client
    app.config["SONY_RECORDER"] = RecordingManager(client, rec_dir)
    app.config["SONY_BROWSER"] = ContentBrowser(client, dl_dir)
    app.register_blueprint(bp)
    return app
