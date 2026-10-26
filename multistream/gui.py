"""The desktop app: the control panel in its own window.

    SCCMultistream.exe                 the built app
    python -m multistream.gui          the same, from source (needs: pip install pywebview)
"""

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
import webbrowser

from . import procs
from .app import StreamApp, start, stop
from .config import DATA_DIR, PORT, tool
from .log import log
from .relay import Ingest, Relay
from .settings import CROP, Settings, new_destination

TITLE = "SCC Multistream"


def message_box(text, title=TITLE, error=False):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10 if error else 0x40)
    else:
        print(f"{title}: {text}")


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            return "relay" in json.loads(r.read())
    except Exception:
        return False


def smoke_test(port, rtmp_port=19350, api_port=19987, timeout=60):
    """Used by the build: the packaged app serves its page, and the relay
    works end to end. A test pattern goes into the RTMP server, and two
    destinations (copied, and cropped to vertical) point back at the same
    server, so both must come out the other side. Writes smoke-test.txt
    next to the app; True if all went well."""
    results, ok = [], True

    def check(name, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        results.append(f"{'OK  ' if passed else 'FAIL'} {name}{f' ({detail})' if detail else ''}")

    work = tempfile.mkdtemp(prefix="sccms-smoke-")
    settings = Settings(os.path.join(work, "config.json"))
    settings.data["destinations"] = [
        new_destination("custom", id="copy", name="Copy", enabled=True, server=f"rtmp://127.0.0.1:{rtmp_port}/out", key="copy"),
        new_destination("custom", id="crop", name="Crop", enabled=True, server=f"rtmp://127.0.0.1:{rtmp_port}/out", key="crop",
                        video=CROP, bitrate=2000),
    ]
    app = StreamApp(settings, Relay(settings, Ingest(rtmp_port=rtmp_port, api_port=api_port, data_dir=work)))
    publisher = None
    try:
        start(app, port)
        for path in ("/", "/api/state", "/update.js", "/fonts/Inter.ttf", "/assets/st-clair-saints.png", "/assets/platforms/youtube.png"):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                    check(path, True, f"{len(r.read())} bytes")
            except Exception as e:
                check(path, False, str(e))
        try:
            import webview  # noqa: F401
            if sys.platform == "win32":
                import webview.platforms.winforms  # noqa: F401
            check("window library", True)
        except Exception as e:
            check("window library", False, repr(e))
        for name in ("ffmpeg", "mediamtx"):
            check(f"{name} bundled", os.path.isfile(tool(name)), tool(name))

        publisher = procs.popen([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-re",
                                 "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                                 "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-pix_fmt", "yuv420p",
                                 "-c:a", "aac", "-t", str(timeout + 10), "-f", "flv",
                                 f"rtmp://127.0.0.1:{rtmp_port}/live/main"])
        deadline = time.monotonic() + timeout
        state = {}
        while time.monotonic() < deadline:
            state = app.relay.state()
            outs = {o["id"]: o for o in state["outputs"]}
            if all(outs.get(i, {}).get("status") == "live" for i in ("copy", "crop")) \
                    and {"out/copy", "out/crop"} <= set(app.relay.ingest.paths):
                break
            time.sleep(1)
        check("OBS stream received", state.get("ingest", {}).get("status") == "live",
              f"{state.get('ingest', {}).get('kbps')} kbps, {state.get('ingest', {}).get('tracks')}")
        outs = {o["id"]: o for o in state.get("outputs", [])}
        for i in ("copy", "crop"):
            o = outs.get(i, {})
            arrived = f"out/{i}" in app.relay.ingest.paths
            check(f"{i} output live", o.get("status") == "live" and arrived,
                  f"{o.get('status')}: {o.get('message') or ''} {o.get('kbps')} kbps".strip())
        check("crop encoder", bool(app.relay.encoder), app.relay.encoder or "none")
    except Exception as e:
        check("smoke test", False, f"{type(e).__name__}: {e}")
    finally:
        procs.stop(publisher)
        stop(app)
    with open(os.path.join(DATA_DIR, "smoke-test.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(results + ["PASSED" if ok else "FAILED"]) + "\n")
    return ok


def main():
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--debug", action="store_true", help="allow right-click > Inspect in the window")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.smoke_test:
        return 0 if smoke_test(args.port) else 1

    url = f"http://127.0.0.1:{args.port}/"
    if already_running(args.port):
        # One relay at a time: open another window onto the one that's running.
        own, app = False, None
    else:
        own, app = True, StreamApp()
        try:
            start(app, args.port)
        except OSError as e:
            message_box(f"Couldn't start on port {args.port}:\n{e}", error=True)
            return 1

    try:
        import webview
        version = app.updater.current if app else ""
        window = webview.create_window(f"{TITLE} {version}".strip(), url, width=1280, height=760,
                                       min_size=(900, 560), background_color="#08120c")
        if app:
            app.updater.on_exit = window.destroy
        webview.start(debug=args.debug)
    except Exception as e:
        log(f"Couldn't open the app window ({e!r}); using the web browser instead", "warn")
        webbrowser.open(url)
        if own:
            message_box("SCC Multistream has opened in your web browser.\n\nIt keeps streaming until you close this box.")
    finally:
        if own:
            stop(app)
    return 0


if __name__ == "__main__":
    sys.exit(main())
