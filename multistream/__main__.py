"""Run the relay and its control panel without a window:  python -m multistream
(then open http://127.0.0.1:8775/)."""

import time

from .app import StreamApp, start, stop
from .config import PORT

if __name__ == "__main__":
    app = StreamApp()
    start(app, PORT)
    print("  Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop(app)
