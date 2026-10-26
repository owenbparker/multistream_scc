"""The app's log: what used to scroll past in the command window, kept in
memory so the desktop app can show it."""

import collections
import itertools
import threading
import time

_lines = collections.deque(maxlen=500)
_ids = itertools.count(1)
_lock = threading.Lock()


def log(message, level="info"):
    entry = {"id": next(_ids), "time": time.strftime("%H:%M:%S"), "level": level, "message": message}
    with _lock:
        _lines.append(entry)
    # Also print, for when it's run from a terminal (does nothing in the .exe).
    print(f"[{entry['time']}] {message}")


def since(after_id=0):
    with _lock:
        return [e for e in _lines if e["id"] > after_id]
