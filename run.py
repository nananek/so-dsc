"""Dev entry point.

    SODSC_HOST=192.168.122.1 python run.py

If SODSC_HOST is unset, the app does an SSDP discovery on startup.
"""
from app import create_app

app = create_app()


if __name__ == "__main__":
    import os
    host = os.environ.get("HOST", "127.0.0.1")
    # Avoid 5000: macOS Monterey+ AirPlay Receiver squats on it (returns 403
    # to anything that's not an AirTunes request, and wins the route even
    # when another process binds 127.0.0.1:5000 specifically).
    port = int(os.environ.get("PORT", "5050"))
    # Threaded but no reloader: the reloader spawns two processes, which would
    # double-open the liveview HTTP stream and confuse the camera.
    app.run(host=host, port=port, threaded=True, use_reloader=False)
