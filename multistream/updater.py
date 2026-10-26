"""Self-update for the desktop app, from GitHub releases.

On launch the app asks GitHub for the latest release of this repository.
If it's newer than the running copy, the control panel offers it. Installing
downloads the release zip (checking its SHA-256 when GitHub provides one),
unpacks it next to the app, then hands over to a small script that waits
for the app to close, copies the new files in and starts the new version.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile

from .config import DATA_DIR, FROZEN
from .log import log
from .version import VERSION

REPO = "owenbparker/multistream_scc"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "SCCMultistream-windows.zip"
UPDATE_DIR = os.path.join(DATA_DIR, "_update")


def parse_version(text):
    """"v1.0.12" -> (1, 0, 12). Anything unnumbered (a dev build) -> (0,)."""
    numbers = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in numbers) if numbers and re.match(r"v?\d", text or "") else (0,)


def is_newer(latest, current):
    return parse_version(latest) > parse_version(current)


class Updater:
    def __init__(self):
        self.current = VERSION
        self.latest = None
        self.notes = ""
        self.asset = None
        self.status = "idle"      # idle, checking, downloading, installing, error
        self.check_error = None   # why the last check for updates failed
        self.progress = 0
        self.error = None
        # Only the packaged app can replace itself.
        self.can_install = FROZEN and sys.platform == "win32"
        self.on_exit = None       # set by the window, to close it for the restart
        self.work_dir = UPDATE_DIR
        self._lock = threading.Lock()

    def info(self):
        return {"current": self.current, "latest": self.latest, "notes": self.notes,
                "available": bool(self.latest and self.asset and is_newer(self.latest, self.current)),
                "can_install": self.can_install, "status": self.status,
                "progress": self.progress, "error": self.error, "check_error": self.check_error}

    # --- checking --------------------------------------------------------

    def check_in_background(self):
        threading.Thread(target=self.check, daemon=True).start()

    def check(self):
        self.status, self.error, self.check_error = "checking", None, None
        try:
            request = urllib.request.Request(LATEST_URL, headers={
                "Accept": "application/vnd.github+json", "User-Agent": f"SCC-Multistream/{self.current}"})
            with urllib.request.urlopen(request, timeout=10) as r:
                release = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            self.status = "idle"
            if e.code == 404:
                log("No releases published on GitHub yet")
            else:
                self.check_error = f"GitHub said {e.code}"
                log(f"Couldn't check for updates (GitHub said {e.code})", "warn")
            return
        except Exception as e:
            self.status = "idle"
            self.check_error = str(getattr(e, "reason", e))
            log(f"Couldn't check for updates ({self.check_error})", "warn")
            return
        self.latest = release.get("tag_name")
        self.notes = (release.get("body") or "").strip()
        self.asset = next((a for a in release.get("assets", []) if a.get("name") == ASSET_NAME), None)
        self.status = "idle"
        if self.info()["available"]:
            log(f"Update available: {self.latest} (you have {self.current})")
        else:
            log(f"SCC Multistream {self.current} is up to date")

    # --- installing ------------------------------------------------------

    def install_in_background(self):
        if not self.can_install:
            raise ValueError("Updates can only be installed in the desktop app")
        if not self.info()["available"]:
            raise ValueError("No update available")
        with self._lock:
            if self.status in ("downloading", "installing"):
                return
            self.status, self.progress, self.error = "downloading", 0, None
        threading.Thread(target=self._install, daemon=True).start()

    def _install(self):
        try:
            zip_path = self._download()
            self.status = "installing"
            new_dir = self._unpack(zip_path)
            script = self._write_script(new_dir)
            log(f"Installing {self.latest} - SCC Multistream will restart")
            # Hidden console, own process group so it outlives the app.
            subprocess.Popen(["cmd", "/c", script], cwd=os.path.dirname(os.path.abspath(sys.executable)),
                             creationflags=0x08000000 | 0x00000200)
            (self.on_exit or (lambda: os._exit(0)))()
        except Exception as e:
            self.status, self.error = "error", f"{type(e).__name__}: {e}"
            log(f"Update failed: {self.error}", "error")

    def _download(self):
        # A fresh folder per attempt: a previous update's files can still be in
        # use (or locked) for a while, and must never block this one.
        os.makedirs(UPDATE_DIR, exist_ok=True)
        for old in os.listdir(UPDATE_DIR):
            shutil.rmtree(os.path.join(UPDATE_DIR, old), ignore_errors=True)
        self.work_dir = os.path.join(UPDATE_DIR, f"{self.latest}-{int(time.time())}")
        os.makedirs(self.work_dir)
        path = os.path.join(self.work_dir, ASSET_NAME)
        total = self.asset.get("size") or 0
        digest = hashlib.sha256()
        request = urllib.request.Request(self.asset["browser_download_url"],
                                         headers={"User-Agent": f"SCC-Multistream/{self.current}"})
        with urllib.request.urlopen(request, timeout=30) as r, open(path, "wb") as f:
            done = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if total:
                    self.progress = min(99, int(done * 100 / total))
        expected = (self.asset.get("digest") or "").removeprefix("sha256:")
        if expected and digest.hexdigest() != expected:
            raise ValueError("Download didn't match GitHub's checksum; try again")
        self.progress = 100
        return path

    def _unpack(self, zip_path):
        new_dir = os.path.join(self.work_dir, "new")
        with zipfile.ZipFile(zip_path) as z:
            for name in z.namelist():  # refuse paths that climb out of the folder
                target = os.path.normpath(os.path.join(new_dir, name))
                if not target.startswith(os.path.normpath(new_dir) + os.sep):
                    raise ValueError(f"Unexpected file in update: {name}")
            z.extractall(new_dir)
        if not os.path.isfile(os.path.join(new_dir, "SCCMultistream.exe")):
            raise ValueError("The update doesn't contain SCCMultistream.exe")
        return new_dir

    def _write_script(self, new_dir):
        app_dir = os.path.dirname(os.path.abspath(sys.executable))
        script = os.path.join(self.work_dir, "apply-update.bat")
        with open(script, "w", encoding="utf-8") as f:
            f.write(f"""@echo off
rem SCC Multistream updater: waits for the app to close, copies in the new
rem version, then restarts it.
:wait
tasklist /FI "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul && (ping -n 2 127.0.0.1 >nul & goto wait)
robocopy "{new_dir}\\_internal" "{app_dir}\\_internal" /MIR /R:3 /W:1 /NFL /NDL /NJH /NJS /NP >nul
robocopy "{new_dir}" "{app_dir}" /XD _internal /R:3 /W:1 /NFL /NDL /NJH /NJS /NP >nul
start "" "{app_dir}\\SCCMultistream.exe"
""")
        return script


def clean_up_old_update():
    """Remove the previous update's download once the new version is running.
    Waits a bit first: the script that just installed us may still be finishing."""
    def clean():
        time.sleep(30)
        shutil.rmtree(UPDATE_DIR, ignore_errors=True)
    if FROZEN and os.path.isdir(UPDATE_DIR):
        threading.Thread(target=clean, daemon=True).start()
