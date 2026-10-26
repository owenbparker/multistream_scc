"""The saved settings (config.json next to the app): where each platform's
stream goes and whether it's on.

Stream keys are kept here in plain text, on this PC only; the control panel
is only reachable from this PC.
"""

import copy
import json
import os
import re
import threading
import uuid

from .config import SETTINGS_FILE
from .log import log

# Video modes for a destination.
COPY = "copy"           # OBS's stream as it is (no re-encode, almost no CPU)
CROP = "crop"           # the middle of OBS's 16:9 picture as 9:16, re-encoded
VERTICAL = "vertical"   # OBS's second, vertical output (stream key "vertical")
VIDEO_MODES = (COPY, CROP, VERTICAL)

PLATFORMS = {
    "youtube": {"name": "YouTube", "server": "rtmp://a.rtmp.youtube.com/live2",
                "help": "YouTube Studio > Go live > Stream > Stream key"},
    "twitch": {"name": "Twitch", "server": "rtmp://live.twitch.tv/app",
               "help": "Twitch Creator Dashboard > Settings > Stream > Primary Stream key"},
    "x": {"name": "X", "server": "",
          "help": "X Media Studio > Producer > Sources: create a source and copy its RTMP URL and key"},
    "tiktok": {"name": "TikTok", "server": "", "video": CROP,
               "help": "TikTok LIVE Studio / LIVE Center > Stream key: copy the Server URL and Stream key (it can change every time you go live)"},
    "facebook": {"name": "Facebook", "server": "rtmps://live-api-s.facebook.com:443/rtmp/",
                 "help": "Facebook Live Producer > Streaming software > Stream key (use a persistent key)"},
    "custom": {"name": "RTMP", "server": "", "help": "Any RTMP or RTMPS server and its stream key"},
}
DEFAULT_ORDER = ("youtube", "twitch", "x", "tiktok", "facebook")


def new_destination(platform, **fields):
    p = PLATFORMS.get(platform, PLATFORMS["custom"])
    d = {"id": platform if platform != "custom" else "rtmp-" + uuid.uuid4().hex[:6],
         "platform": platform if platform in PLATFORMS else "custom",
         "name": p["name"], "enabled": False, "server": p["server"], "key": "",
         "video": p.get("video", COPY), "bitrate": 6000,
         "account": "",       # who's logged in, when the key came from logging in
         "chat": ""}          # Twitch channel / YouTube channel to show the chat of
    d.update(fields)
    return d


def defaults():
    return {"destinations": [new_destination(p) for p in DEFAULT_ORDER],
            "lan": False,          # let other PCs on the network stream in
            "encoder": "auto",     # for re-encoded (cropped) outputs
            "tokens": {},          # destination id -> login refresh token (Twitch viewers)
            # Stream profiles: which platforms to send to. The chosen one's
            # platforms are the ones switched on.
            "profiles": [{"name": "Main", "platforms": []}],
            "profile": "Main"}


def clean_destination(d, old=None):
    """A destination as sent by the control panel -> a saved one. Raises ValueError."""
    out = dict(old or new_destination(str(d.get("platform") or "custom")))
    for field in ("name", "server", "key", "account", "chat"):
        if field in d:
            out[field] = str(d[field] or "").strip()
    if "key" in d and "account" not in d and old and out["key"] != old.get("key"):
        out["account"] = ""    # a pasted key: no longer the logged-in account's
    if "enabled" in d:
        out["enabled"] = bool(d["enabled"])
    if "video" in d:
        if d["video"] not in VIDEO_MODES:
            raise ValueError(f"Unknown video mode: {d['video']}")
        out["video"] = d["video"]
    if "bitrate" in d:
        try:
            out["bitrate"] = max(500, min(20000, int(d["bitrate"])))
        except (TypeError, ValueError):
            raise ValueError("Bitrate must be a number (kbps)")
    if out["server"] and not re.match(r"^rtmps?://", out["server"], re.I):
        raise ValueError("The server should start with rtmp:// or rtmps://")
    if not out["name"]:
        out["name"] = PLATFORMS.get(out["platform"], PLATFORMS["custom"])["name"]
    return out


def target_url(d):
    """The full address FFmpeg pushes to: server + stream key."""
    server, key = d.get("server", "").strip(), d.get("key", "").strip()
    if not key:
        return server
    return server.rstrip("/") + "/" + key


class Settings:
    def __init__(self, path=SETTINGS_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.data = defaults()
        self.load()

    def load(self):
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError) as e:
            log(f"Couldn't read {os.path.basename(self.path)} ({e}); using defaults", "warn")
            return
        data = defaults()
        for k in ("lan", "encoder", "tokens", "profiles", "profile"):
            if k in saved:
                data[k] = saved[k]
        if isinstance(saved.get("destinations"), list):
            data["destinations"] = [{**new_destination(d.get("platform", "custom")), **d}
                                    for d in saved["destinations"] if isinstance(d, dict) and d.get("id")]
        if not saved.get("profiles"):   # from before profiles: one with what's switched on
            data["profiles"] = [{"name": "Main", "platforms": [d["id"] for d in data["destinations"] if d["enabled"]]}]
            data["profile"] = "Main"
        self.data = data

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self.data)

    def destinations(self):
        return self.snapshot()["destinations"]

    def update_destination(self, d):
        """Add (no id or unknown id) or change one destination."""
        with self._lock:
            dests = self.data["destinations"]
            i = next((i for i, x in enumerate(dests) if x["id"] == d.get("id")), None)
            if i is None:
                new = clean_destination(d)
                dests.append(new)
            else:
                new = dests[i] = clean_destination(d, dests[i])
            self.save()
            return new

    def remove_destination(self, dest_id):
        with self._lock:
            self.data["destinations"] = [d for d in self.data["destinations"] if d["id"] != dest_id]
            for p in self.data["profiles"]:
                p["platforms"] = [i for i in p["platforms"] if i != dest_id]
            self.save()

    # --- Stream profiles -----------------------------------------------------

    def _profile(self, name):
        p = next((p for p in self.data["profiles"] if p["name"] == name), None)
        if p is None:
            raise ValueError(f"No profile called {name}")
        return p

    def save_all(self, changes, profile=None):
        """The control panel's Save: every card's edits (enabled included) in
        one go, and the platforms that are on stored in the chosen profile."""
        with self._lock:
            dests = self.data["destinations"]
            cleaned = {}
            for change in changes:
                i = next((i for i, x in enumerate(dests) if x["id"] == change.get("id")), None)
                if i is None:
                    raise ValueError(f"Unknown destination: {change.get('id')}")
                cleaned[i] = clean_destination(change, dests[i])   # all checked before any is applied
            for i, d in cleaned.items():
                dests[i] = d
            if profile is not None:
                self._profile(profile)
                self.data["profile"] = profile
            p = self._profile(self.data["profile"])
            p["platforms"] = [d["id"] for d in dests if d["enabled"]]
            self.save()

    def select_profile(self, name):
        """Switch on exactly the chosen profile's platforms."""
        with self._lock:
            p = self._profile(name)
            for d in self.data["destinations"]:
                d["enabled"] = d["id"] in p["platforms"]
            self.data["profile"] = name
            self.save()

    def add_profile(self, name, platforms):
        name = (name or "").strip()
        with self._lock:
            if not name:
                raise ValueError("Give the profile a name")
            if any(p["name"].lower() == name.lower() for p in self.data["profiles"]):
                raise ValueError(f"There's already a profile called {name}")
            ids = {d["id"] for d in self.data["destinations"]}
            self.data["profiles"].append({"name": name, "platforms": [i for i in platforms if i in ids]})
            self.save()

    def delete_profile(self, name):
        with self._lock:
            if len(self.data["profiles"]) <= 1:
                raise ValueError("Keep at least one profile")
            self._profile(name)
            self.data["profiles"] = [p for p in self.data["profiles"] if p["name"] != name]
            if self.data["profile"] == name:
                self.data["profile"] = self.data["profiles"][0]["name"]
            self.save()

    def token(self, dest_id):
        with self._lock:
            return self.data.get("tokens", {}).get(dest_id, "")

    def set_token(self, dest_id, value):
        with self._lock:
            tokens = self.data.setdefault("tokens", {})
            if tokens.get(dest_id, "") != value:
                tokens[dest_id] = value
                self.save()

    def set(self, **values):
        with self._lock:
            for k, v in values.items():
                if k not in ("lan", "encoder"):
                    raise ValueError(f"Unknown setting: {k}")
                self.data[k] = v
            self.save()
