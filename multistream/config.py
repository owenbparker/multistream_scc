"""Where the app's files are, and its ports."""

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROZEN = getattr(sys, "frozen", False)
# Read-only files shipped with the app (the page, MediaMTX and FFmpeg).
RESOURCE_DIR = getattr(sys, "_MEIPASS", ROOT) if FROZEN else ROOT
# Things the app writes (settings, MediaMTX's config, updates).
DATA_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else ROOT
WEB_DIR = os.path.join(RESOURCE_DIR, "web")
BIN_DIR = os.path.join(RESOURCE_DIR, "bin")
SETTINGS_FILE = os.path.join(DATA_DIR, "config.json")

PORT = 8775         # the control panel
RTMP_PORT = 1935    # where OBS streams to
API_PORT = 9987     # MediaMTX's API (this PC only)


def tool(name):
    """Path to a bundled program (bin/mediamtx.exe, bin/ffmpeg.exe), or the
    one on PATH when running from source without bin/."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    bundled = os.path.join(BIN_DIR, exe)
    if os.path.isfile(bundled):
        return bundled
    return shutil.which(name) or bundled
